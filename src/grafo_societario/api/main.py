"""A aplicação e o que ela declara sobre si mesma.

O carregamento dos artefatos não mora aqui: está em `deps.py`, junto da regra de
que a partida morre em vez de a aplicação subir mentindo. Se o `/health`
responde, o grafo está mapeado e conferido — e é isso que torna a resposta dele
digna de confiança.

## O que o `/health` responde, e por que o SHA-256 está lá

A competência diz de qual mês é o dado. A **soma** diz qual execução o produziu —
e é ela que responde "qual build está no ar" sem adivinhação. Duas construções da
mesma competência sobre o mesmo silver produzem os mesmos bytes, garantia que o
projeto sustenta desde a Fase 4, então soma diferente significa dado diferente.

Calcular as somas custa 0,26 s sobre os 416 MB, com o cache quente. É verificação
síncrona de propósito: ela é a única forma de detectar corrupção antes do usuário,
e a leitura sequencial ainda aquece o cache que a primeira consulta usaria em
faltas de página aleatórias. O custo por despertar num free tier que hiberna é
medido no commit 44, contra o disco real. As somas são calculadas **na partida** e
guardadas no acervo: o `/health` devolve o que já foi medido, e não relê 416 MB a
cada chamada de health check.

## A aplicação não importa DuckDB, SciPy nem leitor de Parquet

É a mesma regra dos módulos que ela consome, e há teste em processo limpo
exigindo isso. A imagem que responde consulta não carrega a máquina que produziu o
artefato.
"""

from __future__ import annotations

from typing import Any

from fastapi import Depends, FastAPI

from grafo_societario import __version__
from grafo_societario.api import caminho, empresa, vizinhanca
from grafo_societario.api.deps import AcervoDep, ciclo
from grafo_societario.api.erros import registrar_tratadores
from grafo_societario.api.limite import limitar
from grafo_societario.config import Config


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
    registrar_tratadores(app)
    # O limitador entra nas rotas de consulta e **não** no `/health`: a
    # plataforma o consulta para decidir se manda tráfego, e limitá-lo faria o
    # limitador derrubar a própria instância.
    consulta = [Depends(limitar)]
    app.include_router(caminho.roteador, dependencies=consulta)
    app.include_router(vizinhanca.roteador, dependencies=consulta)
    app.include_router(empresa.roteador, dependencies=consulta)

    @app.get("/health", tags=["operação"])
    def health(acervo: AcervoDep) -> dict[str, Any]:
        """Qual dado está no ar, e de qual execução ele veio.

        Só responde se o acervo carregou: a aplicação não sobe sem ele.
        """
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
            "limite": {
                "por_minuto": acervo.config.limite_por_minuto,
                "proxies_confiaveis": acervo.config.proxies_confiaveis,
            },
            "segundos_de_partida": round(acervo.segundos_de_partida, 3),
        }

    return app


app = criar_aplicacao()
"""A instância que o `uvicorn grafo_societario.api.main:app` sobe."""
