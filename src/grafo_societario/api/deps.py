"""O acervo: carregado uma vez na partida, injetado em toda rota, lido por muitas.

## "Uma vez" é sobre reabertura, não sobre leitura de disco

O critério do plano diz "a segunda requisição não relê disco". Está errado, e o
erro é o mesmo do ADR-002: `mmap` **relê disco o tempo todo**, por falta de
página, e é assim que ele foi projetado. A medição da Fase 4 mostrou o tamanho
disso — abrir custa 0,07 MiB residentes, e cem mil acessos aleatórios trazem
+110,10 MiB, quase o artefato inteiro.

O que este módulo garante, então, é o que de fato importa e o que de fato dá para
afirmar: **o artefato não é reaberto**. `carregar_acervo` roda uma vez, nenhum
arquivo da pasta do grafo é aberto de novo, e o SHA-256 dos 416 MB não é
recalculado por requisição. Foi por isso que o `mmap` foi escolhido — ele
economiza **partida**, não memória —, e confundir as duas coisas faz alguém
"otimizar" a falta de página um dia, trocando o barato pelo caro.

O teste correspondente afirma reabertura, e por isso ele tem controle negativo:
uma rota que chama `carregar_acervo` a cada requisição precisa ser vista pelo
contador, senão contar zero não prova nada.

## Concorrência: o acervo é lido por várias threads ao mesmo tempo

O uvicorn manda endpoint síncrono para um threadpool, então as estruturas daqui
respondem a requisições simultâneas. Elas aguentam porque **não há estado
mutável**: os mapeamentos abrem em modo somente leitura, as dataclasses são
congeladas, e todo método devolve fatia ou valor novo.

O ponto que exigiu exame é o catálogo, que descomprime um bloco `zlib` para ler
um nome. **Não há cache de bloco descomprimido, e a ausência é decisão medida**,
contra o artefato real de 2026-06:

- ler um nome custa **343 µs**, e o mesmo nó relido custa os mesmos 343 µs;
- um caminho de 21 nós alterna empresa e pessoa, e pessoa física não tem nome no
  artefato publicável — são ~11 descompressões, **~3,8 ms** na resposta inteira;
- com 8 threads o custo por nome cai para **77 µs**, 4,4 vezes melhor que serial,
  porque o `zlib` solta a GIL enquanto descomprime.

O último número é o que decide. Um cache precisaria de sincronização, e o lock
serializaria justamente o trecho que hoje é o único que escala de verdade — para
economizar 3,8 ms numa resposta. Cache aqui entra quando houver medição que o
justifique, e aí ele nasce com a sincronização junto.

## A partida morre em vez de a aplicação mentir depois

Um processo morto você percebe. Uma aplicação que sobe, responde `200` no
`/health` e só descobre que o artefato está ausente quando alguém consulta, você
descobre pelo usuário — depois de o balanceador já ter mandado tráfego para ela.

Por isso tudo é carregado e conferido antes de a aplicação existir: artefato
ausente, arrays de execuções diferentes ou blob truncado derrubam a partida.
"""

from __future__ import annotations

import logging
import time
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import dataclass
from typing import Annotated, Any

import numpy as np
from fastapi import Depends, FastAPI, Request

from grafo_societario.config import Config, carregar_config
from grafo_societario.graph.artefatos import somas_dos_artefatos
from grafo_societario.graph.catalogo import Catalogo, abrir_catalogo
from grafo_societario.graph.csr import Grafo, abrir_grafo, carregar_componentes

logger = logging.getLogger(__name__)


class ErroDeAcervo(RuntimeError):
    """Falha em carregar ou em alcançar o acervo."""


class ErroDePartida(ErroDeAcervo):
    """A aplicação não pode subir com o que está em disco."""


class AcervoIndisponivelError(ErroDeAcervo):
    """A rota foi alcançada sem o acervo ter sido carregado."""


@dataclass(frozen=True)
class Acervo:
    """Tudo o que a resposta precisa, carregado uma vez e conferido.

    Congelada porque é compartilhada entre threads: o que muda depois da partida
    muda debaixo de uma requisição que já estava lendo.
    """

    config: Config
    competencia: str
    grafo: Grafo
    catalogo: Catalogo
    componentes: np.ndarray[Any, np.dtype[np.int32]]
    existencia: np.ndarray[Any, np.dtype[np.int32]]
    somas: dict[str, str]
    segundos_de_partida: float

    @property
    def nos(self) -> int:
        return self.grafo.nos

    @property
    def arestas(self) -> int:
        return self.grafo.posicoes // 2


def carregar_acervo(config: Config, competencia: str | None = None) -> Acervo:
    """Mapeia, confere e devolve o acervo — ou levanta antes de a aplicação subir.

    A conferência que só existe aqui é a **cruzada**: cada artefato já valida a
    própria consistência interna, e o que ninguém vê sozinho é o catálogo
    descrevendo um número de nós diferente do CSR. Artefatos de execuções
    diferentes respondem normalmente e apontam para o nó errado.
    """
    alvo = competencia or config.competencia
    comeco = time.monotonic()
    origem = config.data_dir / "grafo" / alvo

    grafo = abrir_grafo(config, alvo)
    catalogo = abrir_catalogo(config, alvo)
    componentes = carregar_componentes(config, alvo, nos=grafo.nos)

    if catalogo.nos != grafo.nos:
        raise ErroDePartida(
            f"O catálogo descreve {catalogo.nos:,} nós e o CSR tem {grafo.nos:,}. Os dois vieram "
            "de execuções diferentes, e cada índice passaria a devolver o metadado de outro nó."
        )

    existencia_npy = origem / "existencia.npy"
    if not existencia_npy.exists():
        raise ErroDePartida(
            f"Falta {existencia_npy}. Sem ele não há como distinguir empresa que não existe no "
            "recorte de empresa que existe e não tem vínculo — e as duas respostas são "
            "diferentes."
        )
    existencia = np.load(existencia_npy, mmap_mode="r")

    acervo = Acervo(
        config=config,
        competencia=alvo,
        grafo=grafo,
        catalogo=catalogo,
        componentes=componentes,
        existencia=existencia,
        somas=somas_dos_artefatos(origem),
        segundos_de_partida=time.monotonic() - comeco,
    )
    logger.info(
        "acervo carregado",
        extra={
            "competencia": alvo,
            "uf_alvo": config.uf_alvo,
            "nos": acervo.nos,
            "arestas": acervo.arestas,
            "empresas": int(catalogo.cnpj_ordenado.size),
            "existencia": int(existencia.size),
            "artefatos": len(acervo.somas),
            "segundos_de_partida": round(acervo.segundos_de_partida, 2),
        },
    )
    return acervo


@asynccontextmanager
async def ciclo(app: FastAPI) -> AsyncIterator[None]:
    """Carrega antes de aceitar tráfego, e deixa a exceção subir se algo falta."""
    config = getattr(app.state, "config", None) or carregar_config()
    app.state.acervo = carregar_acervo(config)
    yield


def obter_acervo(request: Request) -> Acervo:
    """O acervo da aplicação, para as rotas declararem com `Depends`.

    Existe para que nenhuma rota alcance `app.state` por conta própria. A
    diferença aparece quando os endpoints saem para módulos separados, nos
    commits seguintes: com a dependência, cada rota declara o que precisa e a
    suíte injeta outro acervo sem subir aplicação nenhuma; sem ela, cada rota
    fecha sobre a instância de `FastAPI` e só existe dentro dela.

    Falta de acervo aqui não é condição de execução, é erro de montagem — rota
    servida sem o `lifespan` ter rodado. Levanta em vez de devolver `None`,
    porque `None` viraria `AttributeError` fundo adentro, longe da causa.
    """
    acervo: Acervo | None = getattr(request.app.state, "acervo", None)
    if acervo is None:
        raise AcervoIndisponivelError(
            "A rota foi alcançada sem o acervo carregado. Ele é montado no `lifespan`, então "
            "isto é aplicação servida sem ele — em teste, `TestClient` usado fora do `with`."
        )
    return acervo


AcervoDep = Annotated[Acervo, Depends(obter_acervo)]
"""O que as rotas anotam. Alias em vez de `Depends` repetido em cada assinatura:
no dia em que a origem do acervo mudar, muda aqui e em nenhum outro lugar."""
