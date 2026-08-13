"""Métricas derivadas, e a asserção que as mantém derivadas.

O teste que dá nome a este arquivo não confere nenhum número: confere que o
diretório do grafo fica **byte a byte igual** depois de calcular tudo. A regra
"derive, não embarque" é fácil de violar por acidente — basta alguém achar que um
array de grau em disco economiza tempo — e uma regra de orçamento que vive só numa
frase não segura nada.
"""

from __future__ import annotations

from collections import Counter
from pathlib import Path
from typing import Any

import pytest

from grafo_societario.graph.csr import Grafo
from grafo_societario.graph.metrics import (
    calcular_metricas,
    graus,
    hubs,
    tamanhos_de_componente,
)
from test_traversal import abrir, montar_csr

ESTRELA_E_PAR = [(0, 1), (0, 2), (0, 3), (0, 4), (5, 6)]
"""Uma estrela de grau 4 no nó 0, um par solto, e o nó 7 sem aresta."""


@pytest.fixture
def rede(tmp_path: Path) -> tuple[Grafo, Any, Path]:
    config = montar_csr(tmp_path, nos=8, arestas=ESTRELA_E_PAR)
    grafo, componentes = abrir(config)
    return grafo, componentes, config.data_dir / "grafo" / "2026-06"


# ------------------------------------ a regra: zero byte a mais


def test_calcular_metricas_nao_toca_no_artefato(rede: tuple[Grafo, Any, Path]) -> None:
    """A regra do orçamento, afirmada em vez de prometida.

    O artefato fechou a Fase 4 com 11,4% de margem. Um array de grau por nó
    custaria 40,7 MiB — um terço do que sobrou — para guardar um número que já
    está em `indptr`.
    """
    grafo, componentes, pasta = rede
    antes = {p.name: (p.stat().st_size, p.read_bytes()) for p in sorted(pasta.iterdir())}

    calcular_metricas(grafo, componentes)

    depois = {p.name: (p.stat().st_size, p.read_bytes()) for p in sorted(pasta.iterdir())}
    assert depois == antes, "métrica não persiste: ela deriva"


# ------------------------------------ grau derivado


def test_o_grau_derivado_bate_com_o_do_grafo(rede: tuple[Grafo, Any, Path]) -> None:
    """Vetorizado e por nó precisam concordar: são a mesma subtração."""
    grafo, _, _ = rede

    vetorizado = graus(grafo)

    assert [int(g) for g in vetorizado] == [grafo.grau(no) for no in range(grafo.nos)]


def test_a_soma_dos_graus_e_o_dobro_das_arestas(rede: tuple[Grafo, Any, Path]) -> None:
    """O aperto de mão: cada aresta é contada por seus dois extremos."""
    grafo, componentes, _ = rede

    metricas = calcular_metricas(grafo, componentes)

    assert int(graus(grafo).sum()) == grafo.posicoes == 2 * metricas.arestas


def test_o_grau_da_estrela_e_o_esperado(rede: tuple[Grafo, Any, Path]) -> None:
    grafo, componentes, _ = rede

    metricas = calcular_metricas(grafo, componentes)

    assert metricas.grau_maximo == 4
    assert metricas.nos == 8
    assert metricas.arestas == 5


def test_a_distribuicao_de_grau_cobre_todos_os_nos(rede: tuple[Grafo, Any, Path]) -> None:
    grafo, componentes, _ = rede

    metricas = calcular_metricas(grafo, componentes)

    assert sum(quantos for _, quantos in metricas.distribuicao_de_grau) == grafo.nos
    assert dict(metricas.distribuicao_de_grau) == dict(Counter(int(g) for g in graus(grafo)))


def test_o_no_sem_aresta_aparece_com_grau_zero(rede: tuple[Grafo, Any, Path]) -> None:
    """Ele existe, tem linha em `indptr`, e o grau dele é zero — não ausência."""
    grafo, componentes, _ = rede

    metricas = calcular_metricas(grafo, componentes)

    assert dict(metricas.distribuicao_de_grau)[0] == 1
    assert grafo.grau(7) == 0


# ------------------------------------ componentes


def test_os_tamanhos_saem_na_ordem_canonica(rede: tuple[Grafo, Any, Path]) -> None:
    """A rotulagem é por tamanho decrescente, então a posição 0 é o gigante."""
    _, componentes, _ = rede

    tamanhos = tamanhos_de_componente(componentes)

    assert list(tamanhos) == sorted(tamanhos, reverse=True)
    assert int(tamanhos[0]) == 5, "a estrela tem 5 nós"


def test_a_particao_cobre_todos_os_nos(rede: tuple[Grafo, Any, Path]) -> None:
    grafo, componentes, _ = rede

    metricas = calcular_metricas(grafo, componentes)

    assert metricas.gigante + metricas.fora_do_gigante == metricas.nos
    assert sum(t * q for t, q in metricas.distribuicao_de_componente) == metricas.nos
    assert sum(q for _, q in metricas.distribuicao_de_componente) == metricas.componentes


# ------------------------------------ ranking de hubs


def test_o_hub_e_o_no_de_maior_grau(rede: tuple[Grafo, Any, Path]) -> None:
    grafo, _, _ = rede

    assert hubs(grafo, 1) == ((0, 4),)


def test_o_ranking_desempata_pelo_menor_indice(tmp_path: Path) -> None:
    """Empate de grau é comum na cauda.

    Sem desempate explícito o ranking mudaria entre versões da biblioteca de
    ordenação, sem o grafo ter mudado — e quem publicasse "os dez maiores" veria a
    lista trocar sozinha.
    """
    grafo, _ = abrir(montar_csr(tmp_path, nos=4, arestas=[(0, 1), (2, 3)]))

    assert hubs(grafo, 4) == ((0, 1), (1, 1), (2, 1), (3, 1))


def test_o_ranking_e_estavel_entre_chamadas(rede: tuple[Grafo, Any, Path]) -> None:
    grafo, _, _ = rede

    assert len({hubs(grafo, 5) for _ in range(10)}) == 1


def test_o_ranking_vem_em_ordem_decrescente(rede: tuple[Grafo, Any, Path]) -> None:
    grafo, _, _ = rede

    ranking = hubs(grafo, 8)

    assert [grau for _, grau in ranking] == sorted((grau for _, grau in ranking), reverse=True)


@pytest.mark.parametrize("quantos", [0, -1])
def test_ranking_sem_tamanho_devolve_vazio(rede: tuple[Grafo, Any, Path], quantos: int) -> None:
    grafo, _, _ = rede

    assert hubs(grafo, quantos) == ()


def test_ranking_maior_que_o_grafo_devolve_todos(rede: tuple[Grafo, Any, Path]) -> None:
    grafo, _, _ = rede

    assert len(hubs(grafo, 999)) == grafo.nos
