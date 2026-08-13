"""Métricas de rede, todas derivadas — nenhuma acrescenta byte ao artefato.

O plano pedia estas métricas "persistidas como Parquet". **Elas não são
persistidas**, e a razão é aritmética: o artefato de deploy fechou a Fase 4 em 443
MB contra o teto de 500, margem de 11,4%. Um array de grau por nó custaria 40,7
MiB e comeria um terço do que sobrou — para guardar um número que já está no
arquivo.

## Cada métrica sai de algo que já existe

- **Grau de um nó** é `indptr[i+1] - indptr[i]`. Dois inteiros, subtração. Gravar
  um array de grau é o mesmo erro da coluna `indice` que o commit 21 derrubou:
  dado redundante com o que está ao lado.
- **Tamanho de componente** é um `bincount` sobre os rótulos, feito na carga. E
  mesmo se fosse gravado seria por componente — 2.841.365 — e não por nó, que são
  10.658.250.
- **Ranking de hubs** é ordenação sobre o grau derivado.

Nada disto é lento o bastante para justificar o disco: derivar o grau dos 10,6
milhões de nós é uma subtração vetorizada sobre um array já mapeado.

## Vetorizado é para análise; por nó é `Grafo.grau`

`graus()` materializa 40,7 MiB em memória, e existe para responder distribuição,
ranking e histograma de uma vez. Quem precisa do grau de **um** nó no caminho de
requisição usa `graph.csr.Grafo.grau`, que lê dois inteiros do mapeamento e não
aloca nada. Confundir os dois traria o artefato inteiro para a memória a cada
consulta.

## Todo grau aqui é relativo ao recorte

Só ingerimos sócios de empresas cuja matriz está na UF alvo. Quem participa de 3
empresas em São Paulo e 40 no Rio aparece com grau 3. O número é **piso, nunca
total** — a mesma ressalva que `vinculos_no_recorte` carrega desde a camada de
identidade, e que precisa sobreviver até o gráfico que alguém publicar.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any, Final

import numpy as np

from grafo_societario.graph.csr import Grafo

logger = logging.getLogger(__name__)

HUBS_REPORTADOS: Final = 20
"""Quantos nós de maior grau entram no resultado por padrão."""


@dataclass(frozen=True)
class Metricas:
    """O retrato da rede, todo derivado dos arrays que já estão em disco."""

    nos: int
    arestas: int

    grau_medio: float
    grau_mediano: int
    grau_maximo: int

    distribuicao_de_grau: tuple[tuple[int, int], ...]
    """`(grau, quantos nós têm esse grau)`, em ordem de grau. Só graus presentes."""

    componentes: int
    gigante: int
    fora_do_gigante: int

    distribuicao_de_componente: tuple[tuple[int, int], ...]
    """`(tamanho, quantos componentes têm esse tamanho)`, em ordem de tamanho."""

    hubs: tuple[tuple[int, int], ...]
    """`(índice do nó, grau)`, do maior para o menor, desempate pelo menor índice.

    O índice é interno e não atravessa a fronteira da API — quem publica hub
    publica o CNPJ ou o hash de identidade, nunca a posição no array."""


def graus(grafo: Grafo) -> np.ndarray[Any, np.dtype[np.int32]]:
    """O grau de todos os nós, derivado de `indptr`.

    Materializa 40,7 MiB no grafo real e é para análise. Grau de um nó só, no
    caminho de resposta, é `Grafo.grau` — dois inteiros lidos do mapeamento.
    """
    return np.diff(np.asarray(grafo.indptr)).astype(np.int32)


def tamanhos_de_componente(
    componentes: np.ndarray[Any, np.dtype[np.int32]],
) -> np.ndarray[Any, np.dtype[np.int64]]:
    """Quantos nós tem cada componente, na ordem canônica dos rótulos.

    Como a rotulagem é por tamanho decrescente, o resultado já sai ordenado — e a
    posição 0 é o gigante. Ver `graph.components`.
    """
    return np.bincount(np.asarray(componentes))


def hubs(grafo: Grafo, quantos: int = HUBS_REPORTADOS) -> tuple[tuple[int, int], ...]:
    """Os nós de maior grau, do maior para o menor.

    O desempate é pelo menor índice, e não pela ordem que o `argsort` devolver:
    empate de grau é comum na cauda, e sem desempate explícito o ranking mudaria
    entre versões da biblioteca sem o grafo ter mudado.
    """
    grau = graus(grafo)
    if not grau.size or quantos < 1:
        return ()
    ordem = np.lexsort((np.arange(grau.size), -grau))[:quantos]
    return tuple((int(no), int(grau[no])) for no in ordem)


def _distribuicao(valores: np.ndarray[Any, np.dtype[Any]]) -> tuple[tuple[int, int], ...]:
    """Histograma exato, sem faixa arbitrada: `(valor, quantas vezes)`."""
    if not valores.size:
        return ()
    contagem = np.bincount(valores)
    presentes = np.flatnonzero(contagem)
    return tuple((int(valor), int(contagem[valor])) for valor in presentes)


def calcular_metricas(
    grafo: Grafo,
    componentes: np.ndarray[Any, np.dtype[np.int32]],
    quantos_hubs: int = HUBS_REPORTADOS,
) -> Metricas:
    """Calcula o retrato da rede sem escrever nada.

    **Não produz artefato.** Tudo aqui sai de `indptr` e dos rótulos que já
    existem, e há teste afirmando que o diretório do grafo fica byte a byte igual
    depois desta chamada.
    """
    grau = graus(grafo)
    tamanhos = tamanhos_de_componente(componentes)
    gigante = int(tamanhos[0]) if tamanhos.size else 0

    resultado = Metricas(
        nos=grafo.nos,
        arestas=grafo.posicoes // 2,
        grau_medio=float(grau.mean()) if grau.size else 0.0,
        grau_mediano=int(np.median(grau)) if grau.size else 0,
        grau_maximo=int(grau.max()) if grau.size else 0,
        distribuicao_de_grau=_distribuicao(grau),
        componentes=int(tamanhos.size),
        gigante=gigante,
        fora_do_gigante=grafo.nos - gigante,
        distribuicao_de_componente=_distribuicao(tamanhos),
        hubs=hubs(grafo, quantos_hubs),
    )
    logger.info(
        "métricas de rede calculadas",
        extra={
            "nos": resultado.nos,
            "arestas": resultado.arestas,
            "grau_medio": round(resultado.grau_medio, 2),
            "grau_maximo": resultado.grau_maximo,
            "componentes": resultado.componentes,
            "gigante": resultado.gigante,
            "bytes_acrescentados": 0,
        },
    )
    return resultado
