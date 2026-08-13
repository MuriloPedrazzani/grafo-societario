"""Busca bidirecional: o caminho, o determinismo, e as três respostas distintas.

O confronto principal é contra o `scipy.sparse.csgraph` sobre o mesmo CSR, e ele
compara **comprimento**, não sequência. Dois algoritmos de caminho mínimo podem
devolver caminhos diferentes e igualmente curtos; exigir a mesma sequência
reprovaria por diferença legítima. O que se afirma sobre a nossa sequência é
separado e mais forte: que ela é válida — toda aresta existe — e que tem
exatamente o comprimento que o scipy diz ser o mínimo.
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path
from typing import Any

import numpy as np
import pytest
from scipy.sparse import csr_matrix
from scipy.sparse.csgraph import connected_components, dijkstra

from grafo_societario.config import Config
from grafo_societario.graph.csr import Grafo, NoForaDaFaixaError, abrir_grafo
from grafo_societario.graph.traversal import (
    ArtefatosDiscordantesError,
    Caminho,
    Desfecho,
    buscar_caminho,
)

SEM_LIMITE = 10_000
"""Alto o bastante para nunca cortar nos grafos destes testes."""


# --------------------------------------------------------- montagem


def montar_csr(destino: Path, nos: int, arestas: list[tuple[int, int]]) -> Config:
    """Escreve um CSR simétrico e ordenado, e os rótulos de componente.

    Os rótulos saem do `csgraph`, que é dependência de teste — o módulo sob teste
    não importa scipy, e há teste separado provando isso.
    """
    pares = {(min(a, b), max(a, b)) for a, b in arestas if a != b}
    origem = np.array([a for a, _ in pares] + [b for _, b in pares], dtype=np.int32)
    alvo = np.array([b for _, b in pares] + [a for a, _ in pares], dtype=np.int32)
    ordem = np.lexsort((alvo, origem))
    origem, alvo = origem[ordem], alvo[ordem]

    indptr = np.zeros(nos + 1, dtype=np.int32)
    indptr[1:] = np.cumsum(np.bincount(origem, minlength=nos))
    matriz = csr_matrix(
        (np.ones(alvo.size, dtype=np.int8), alvo, indptr), shape=(nos, nos), copy=False
    )
    _, rotulos = connected_components(matriz, directed=False, return_labels=True)

    pasta = destino / "grafo" / "2026-06"
    pasta.mkdir(parents=True, exist_ok=True)
    for nome, array in (
        ("indptr.npy", indptr),
        ("indices.npy", alvo.astype(np.int32)),
        ("qualificacoes.npy", np.zeros(alvo.size, dtype=np.int8)),
        ("componentes.npy", rotulos.astype(np.int32)),
    ):
        with (pasta / nome).open("wb") as arquivo:
            np.save(arquivo, array, allow_pickle=False)
    return Config(competencia="2026-06", data_dir=destino, uf_alvo="SP")


def abrir(config: Config) -> tuple[Grafo, Any]:
    grafo = abrir_grafo(config)
    caminho = config.data_dir / "grafo" / "2026-06" / "componentes.npy"
    return grafo, np.load(caminho, mmap_mode="r")


CORRENTE = [(0, 1), (1, 2), (2, 3), (3, 4)]
"""Uma corrente de cinco nós, mais um par solto e um nó sem aresta."""


@pytest.fixture
def corrente(tmp_path: Path) -> tuple[Grafo, Any]:
    return abrir(montar_csr(tmp_path, nos=8, arestas=[*CORRENTE, (5, 6)]))


# --------------------------------------------------------- o caminho


def test_caminho_entre_vizinhos(corrente: tuple[Grafo, Any]) -> None:
    grafo, componentes = corrente

    caminho = buscar_caminho(grafo, componentes, 0, 1, SEM_LIMITE)

    assert caminho.nos == (0, 1)
    assert caminho.saltos == 1
    assert caminho.encontrado


def test_caminho_atravessa_a_corrente(corrente: tuple[Grafo, Any]) -> None:
    grafo, componentes = corrente

    caminho = buscar_caminho(grafo, componentes, 0, 4, SEM_LIMITE)

    assert caminho.nos == (0, 1, 2, 3, 4)
    assert caminho.saltos == 4


def test_mesmo_no_tem_zero_saltos(corrente: tuple[Grafo, Any]) -> None:
    grafo, componentes = corrente

    caminho = buscar_caminho(grafo, componentes, 2, 2, SEM_LIMITE)

    assert caminho.nos == (2,)
    assert caminho.saltos == 0
    assert caminho.encontrado


# ------------------------------- as três respostas, que não podem virar uma


def test_componentes_diferentes_respondem_sem_percorrer(corrente: tuple[Grafo, Any]) -> None:
    """A resposta mais barata, e a mais comum: 87% dos pares caem aqui."""
    grafo, componentes = corrente

    caminho = buscar_caminho(grafo, componentes, 0, 5, SEM_LIMITE)

    assert caminho.desfecho is Desfecho.COMPONENTES_DIFERENTES
    assert caminho.nos == ()
    assert caminho.visitados == 0, "não pode ter percorrido nada"


def test_no_isolado_nao_alcanca_ninguem(corrente: tuple[Grafo, Any]) -> None:
    """O nó 7 não tem aresta: é componente próprio, e o rótulo já responde."""
    grafo, componentes = corrente

    caminho = buscar_caminho(grafo, componentes, 7, 0, SEM_LIMITE)

    assert caminho.desfecho is Desfecho.COMPONENTES_DIFERENTES
    assert caminho.visitados == 0


def test_alem_do_limite_nao_e_ausencia_de_caminho(corrente: tuple[Grafo, Any]) -> None:
    """A distinção que a fase existe para preservar.

    Existe caminho de 0 a 4 — o componente garante. Com limite 3 ele não é
    encontrado, e responder "não há vínculo" seria afirmação falsa sobre empresa
    real: a verdade é "não procurei até lá".
    """
    grafo, componentes = corrente

    caminho = buscar_caminho(grafo, componentes, 0, 4, 3)

    assert caminho.desfecho is Desfecho.ALEM_DO_LIMITE
    assert caminho.nos == ()
    # E o mesmo par, sem limite apertado, tem caminho: é o que torna a distinção
    # verdadeira em vez de nominal.
    assert buscar_caminho(grafo, componentes, 0, 4, SEM_LIMITE).encontrado


def test_o_limite_exato_encontra(corrente: tuple[Grafo, Any]) -> None:
    """Controle positivo do corte: com o limite exato, o caminho aparece."""
    grafo, componentes = corrente

    assert buscar_caminho(grafo, componentes, 0, 4, 4).encontrado


@pytest.mark.parametrize("limite", [0, -1])
def test_limite_nao_positivo_nao_encontra_vizinho(corrente: tuple[Grafo, Any], limite: int) -> None:
    grafo, componentes = corrente

    assert buscar_caminho(grafo, componentes, 0, 1, limite).desfecho is Desfecho.ALEM_DO_LIMITE


# --------------------------------------------- canonização e determinismo


def test_o_caminho_inverso_e_o_reverso_exato(corrente: tuple[Grafo, Any]) -> None:
    """Por construção, e não por coincidência.

    Sem canonizar o par, quem consultasse os dois sentidos veria caminhos
    diferentes de mesmo comprimento — e leria como defeito.
    """
    grafo, componentes = corrente

    ida = buscar_caminho(grafo, componentes, 0, 4, SEM_LIMITE)
    volta = buscar_caminho(grafo, componentes, 4, 0, SEM_LIMITE)

    assert volta.nos == tuple(reversed(ida.nos))


def test_o_caminho_e_estavel_entre_chamadas(corrente: tuple[Grafo, Any]) -> None:
    """Mesma consulta, mesmo caminho. Sem isso não há cache nem demo estável."""
    grafo, componentes = corrente

    resultados = {buscar_caminho(grafo, componentes, 0, 4, SEM_LIMITE).nos for _ in range(20)}

    assert len(resultados) == 1


def test_o_desempate_escolhe_o_mesmo_caminho_entre_iguais(tmp_path: Path) -> None:
    """Dois caminhos mínimos de 0 a 3, por 1 ou por 2. A escolha é sempre a mesma."""
    grafo, componentes = abrir(
        montar_csr(tmp_path, nos=4, arestas=[(0, 1), (0, 2), (1, 3), (2, 3)])
    )

    caminho = buscar_caminho(grafo, componentes, 0, 3, SEM_LIMITE)

    assert caminho.saltos == 2
    assert caminho.nos in {(0, 1, 3), (0, 2, 3)}
    assert all(
        buscar_caminho(grafo, componentes, 0, 3, SEM_LIMITE).nos == caminho.nos for _ in range(20)
    )


# ------------------------------------------ o caminho é mínimo, e é válido


def distancias_do_scipy(grafo: Grafo, origem: int) -> Any:
    matriz = csr_matrix(
        (
            np.ones(grafo.posicoes, dtype=np.int8),
            np.asarray(grafo.indices),
            np.asarray(grafo.indptr),
        ),
        shape=(grafo.nos, grafo.nos),
        copy=False,
    )
    distancias: Any = dijkstra(matriz, directed=False, unweighted=True, indices=[origem])
    return distancias[0]


def caminho_e_valido(grafo: Grafo, caminho: Caminho) -> bool:
    """Toda aresta consecutiva existe de verdade no grafo."""
    return all(
        grafo.sao_vizinhos(int(a), int(b))
        for a, b in zip(caminho.nos, caminho.nos[1:], strict=False)
    )


def test_o_comprimento_confere_com_o_scipy(corrente: tuple[Grafo, Any]) -> None:
    grafo, componentes = corrente
    distancias = distancias_do_scipy(grafo, 0)

    for destino in range(grafo.nos):
        caminho = buscar_caminho(grafo, componentes, 0, destino, SEM_LIMITE)
        esperado = distancias[destino]
        if np.isinf(esperado):
            assert caminho.desfecho is Desfecho.COMPONENTES_DIFERENTES
        else:
            assert caminho.encontrado
            assert caminho.saltos == int(esperado)


def grafo_aleatorio(semente: int, nos: int, arestas: int) -> list[tuple[int, int]]:
    gerador = np.random.default_rng(semente)
    pares = gerador.integers(0, nos, size=(arestas, 2))
    return [(int(a), int(b)) for a, b in pares]


@pytest.mark.parametrize("semente", range(12))
def test_propriedade_do_caminho_em_grafo_aleatorio(tmp_path: Path, semente: int) -> None:
    """Válido, mínimo, simétrico e dentro do limite — sobre grafos gerados.

    A Fase 5 fecha com Hypothesis no commit 31; aqui a geração é semeada, o que já
    cobre topologias que nenhuma fixture escrita à mão produziria.
    """
    nos = 40
    grafo, componentes = abrir(
        montar_csr(tmp_path, nos=nos, arestas=grafo_aleatorio(semente, nos, 55))
    )
    gerador = np.random.default_rng(semente + 1000)

    for origem in range(0, nos, 7):
        distancias = distancias_do_scipy(grafo, origem)
        for destino in gerador.integers(0, nos, size=10):
            alvo = int(destino)
            caminho = buscar_caminho(grafo, componentes, origem, alvo, SEM_LIMITE)
            esperado = distancias[alvo]

            if np.isinf(esperado):
                assert caminho.desfecho is Desfecho.COMPONENTES_DIFERENTES
                continue

            assert caminho.encontrado
            assert caminho.saltos == int(esperado), "não é mínimo"
            assert caminho_e_valido(grafo, caminho), "aresta inexistente no caminho"
            assert caminho.nos[0] == origem and caminho.nos[-1] == alvo

            volta = buscar_caminho(grafo, componentes, alvo, origem, SEM_LIMITE)
            assert volta.nos == tuple(reversed(caminho.nos)), "não é o reverso exato"


@pytest.mark.parametrize("semente", range(6))
def test_o_limite_nunca_e_ultrapassado(tmp_path: Path, semente: int) -> None:
    """Caminho devolvido jamais é mais longo que o pedido."""
    nos = 40
    grafo, componentes = abrir(
        montar_csr(tmp_path, nos=nos, arestas=grafo_aleatorio(semente, nos, 55))
    )

    for limite in (1, 2, 3, 5):
        for origem in range(0, nos, 9):
            for destino in range(0, nos, 11):
                caminho = buscar_caminho(grafo, componentes, origem, destino, limite)
                assert caminho.saltos <= limite
                if caminho.encontrado:
                    assert caminho_e_valido(grafo, caminho)


def test_o_limite_nao_muda_o_caminho_quando_cabe(tmp_path: Path) -> None:
    """Controle negativo do corte: limite folgado devolve o mesmo que limite justo."""
    grafo, componentes = abrir(montar_csr(tmp_path, nos=8, arestas=CORRENTE))

    justo = buscar_caminho(grafo, componentes, 0, 4, 4)
    folgado = buscar_caminho(grafo, componentes, 0, 4, SEM_LIMITE)

    assert justo.nos == folgado.nos


# --------------------------------------------------------- as guardas


@pytest.mark.parametrize(("origem", "destino"), [(-1, 0), (0, 99), (99, 0)])
def test_no_fora_da_faixa_e_recusado(
    corrente: tuple[Grafo, Any], origem: int, destino: int
) -> None:
    grafo, componentes = corrente

    with pytest.raises(NoForaDaFaixaError):
        buscar_caminho(grafo, componentes, origem, destino, SEM_LIMITE)


def test_componente_que_mente_levanta_erro(tmp_path: Path) -> None:
    """Se o rótulo diz que há caminho e a topologia esgota, os artefatos vieram de
    execuções diferentes — e a resposta negativa que sai do rótulo deixou de valer.
    """
    config = montar_csr(tmp_path, nos=4, arestas=[(0, 1), (2, 3)])
    grafo = abrir_grafo(config)
    mentira = np.zeros(grafo.nos, dtype=np.int32)  # diz que está tudo junto

    with pytest.raises(ArtefatosDiscordantesError, match="execuções diferentes"):
        buscar_caminho(grafo, mentira, 0, 2, SEM_LIMITE)


# ------------------------------- a fronteira entre serving e construção


def test_o_caminho_de_resposta_nao_carrega_o_motor_nem_o_scipy() -> None:
    """A regra arquitetural, afirmada em vez de documentada.

    A imagem da Fase 8 responde consulta sobre arrays pré-computados. Carregar
    DuckDB ou SciPy ali seria embarcar a máquina que produziu o artefato dentro da
    que só o lê — e o custo apareceria no tempo de partida, que é justamente o que
    o `mmap` foi escolhido para proteger.
    """
    codigo = (
        "import sys; import grafo_societario.graph.traversal; "
        "print('duckdb' in sys.modules, 'scipy' in sys.modules)"
    )

    saida = subprocess.run(
        [sys.executable, "-c", codigo], capture_output=True, text=True, check=True
    )

    assert saida.stdout.strip() == "False False", (
        f"traversal.py arrastou motor de ETL ou biblioteca científica: {saida.stdout.strip()}"
    )
