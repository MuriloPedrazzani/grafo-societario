"""A aplicação, e a regra de que ela morre na partida em vez de mentir depois.

## Health check verde com grafo não carregado é pior que processo morto

Um processo morto você percebe. Uma aplicação que sobe, responde `200` no
`/health` e só descobre que o artefato está ausente quando alguém consulta, você
descobre pelo usuário — e depois de o balanceador já ter mandado tráfego para ela.

Por isso **tudo é carregado e conferido antes de a aplicação existir**: artefato
ausente, arrays de execuções diferentes ou blob truncado derrubam a partida. Se o
`/health` responde, o grafo está mapeado e conferido.

## O que o `/health` responde, e por que o SHA-256 está lá

A competência diz de qual mês é o dado. A **soma** diz qual execução o produziu —
e é ela que responde "qual build está no ar" sem adivinhação. Duas construções da
mesma competência sobre o mesmo silver produzem os mesmos bytes, garantia que o
projeto sustenta desde a Fase 4, então soma diferente significa dado diferente.

Calcular as somas custa 0,26 s sobre os 416 MB, com o cache quente. É verificação
síncrona de propósito: ela é a única forma de detectar corrupção antes do usuário,
e a leitura sequencial ainda aquece o cache que a primeira consulta usaria em
faltas de página aleatórias. O custo por despertar num free tier que hiberna é
medido no commit 44, contra o disco real.

## A aplicação não importa DuckDB, SciPy nem leitor de Parquet

É a mesma regra dos três módulos que ela consome, e há teste em processo limpo
exigindo isso. A imagem que responde consulta não carrega a máquina que produziu o
artefato.
"""

from __future__ import annotations

import logging
import time
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import dataclass
from typing import Any

import numpy as np
from fastapi import FastAPI

from grafo_societario import __version__
from grafo_societario.config import Config, carregar_config
from grafo_societario.graph.artefatos import somas_dos_artefatos
from grafo_societario.graph.catalogo import Catalogo, abrir_catalogo
from grafo_societario.graph.csr import Grafo, abrir_grafo, carregar_componentes

logger = logging.getLogger(__name__)


class ErroDePartida(RuntimeError):
    """A aplicação não pode subir com o que está em disco."""


@dataclass(frozen=True)
class Acervo:
    """Tudo o que a resposta precisa, carregado uma vez e conferido."""

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


def criar_aplicacao(config: Config | None = None) -> FastAPI:
    """Monta a aplicação. `config` explícito existe para a suíte não depender do
    ambiente da máquina que roda os testes."""
    app = FastAPI(
        title="Grafo Societário",
        version=__version__,
        summary="Caminhos societários entre empresas brasileiras, a partir dos dados abertos "
        "de CNPJ da Receita Federal.",
        lifespan=ciclo,
    )
    if config is not None:
        app.state.config = config

    @app.get("/health", tags=["operação"])
    def health() -> dict[str, Any]:
        """Qual dado está no ar, e de qual execução ele veio.

        Só responde se o acervo carregou: a aplicação não sobe sem ele.
        """
        acervo: Acervo = app.state.acervo
        return {
            "status": "ok",
            "versao": __version__,
            "competencia": acervo.competencia,
            "uf_alvo": acervo.config.uf_alvo,
            "expor_pf": acervo.config.expor_pf,
            "grafo": {
                "nos": acervo.nos,
                "arestas": acervo.arestas,
                "empresas_consultaveis": int(acervo.catalogo.cnpj_ordenado.size),
                "empresas_no_recorte": int(acervo.existencia.size),
            },
            "artefatos": acervo.somas,
            "segundos_de_partida": round(acervo.segundos_de_partida, 3),
        }

    return app


app = criar_aplicacao()
"""A instância que o `uvicorn grafo_societario.api.main:app` sobe."""
