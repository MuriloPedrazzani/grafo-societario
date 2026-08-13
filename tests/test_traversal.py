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
    ORCAMENTO_DE_VISITADOS,
    ArtefatosDiscordantesError,
    Caminho,
    Desfecho,
    PedidoInvalidoError,
    buscar_caminho,
    vizinhanca,
)

SEM_LIMITE = 10_000
"""Alto o bastante para nunca cortar nos grafos destes testes."""

PIOR_CASO_MEDIDO = 99_596
"""Nós visitados pela pior consulta medida no grafo real, em 6.500 amostras."""


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


# ------------------------------------------------- vizinhança de k saltos


TRIANGULO_COM_CAUDA = [(0, 1), (0, 2), (1, 2), (2, 3), (3, 4)]
"""0-1-2 formam um ciclo; 2-3-4 é uma cauda.

A aresta 1-2 liga dois nós do **mesmo nível** a partir de 0. Ela existe no
subgrafo induzido e não existiria na árvore de busca — é o caso que distingue os
dois, e o ciclo é o achado que a árvore esconderia.
"""


@pytest.fixture
def triangulo(tmp_path: Path) -> tuple[Grafo, Any]:
    return abrir(montar_csr(tmp_path, nos=6, arestas=TRIANGULO_COM_CAUDA))


def test_um_salto_traz_o_no_e_os_vizinhos(triangulo: tuple[Grafo, Any]) -> None:
    grafo, _ = triangulo

    bola = vizinhanca(grafo, 0, saltos=1, teto_de_nos=100)

    assert bola.nos == (0, 1, 2)
    assert bola.profundidades == (0, 1, 1)
    assert bola.saltos == 1


def test_zero_salto_traz_so_a_origem(triangulo: tuple[Grafo, Any]) -> None:
    grafo, _ = triangulo

    bola = vizinhanca(grafo, 0, saltos=0, teto_de_nos=100)

    assert bola.nos == (0,)
    assert bola.arestas == ()
    assert not bola.truncada


def test_o_subgrafo_e_induzido_e_nao_a_arvore(triangulo: tuple[Grafo, Any]) -> None:
    """A aresta entre dois nós do mesmo nível entra.

    Árvore de busca sobre 3 nós teria 2 arestas. O induzido tem 3, e a terceira é
    o ciclo — que é justamente o que interessa a quem investiga: duas empresas
    ligadas por um segundo sócio em comum.
    """
    grafo, _ = triangulo

    bola = vizinhanca(grafo, 0, saltos=1, teto_de_nos=100)

    assert bola.arestas == ((0, 1), (0, 2), (1, 2))
    assert len(bola.arestas) > len(bola.nos) - 1, "árvore teria n-1 arestas"


def test_o_grau_e_o_do_grafo_inteiro_e_nao_o_do_recorte(triangulo: tuple[Grafo, Any]) -> None:
    """O nó 2 tem grau 3 no grafo e só 2 arestas dentro da bola de 1 salto.

    Contar as arestas desenhadas daria um número que parece o grau e não é — e a
    borda de qualquer recorte por distância tem esse problema.
    """
    grafo, _ = triangulo

    bola = vizinhanca(grafo, 0, saltos=1, teto_de_nos=100)

    graus = dict(zip(bola.nos, bola.graus, strict=True))
    assert graus[2] == 3
    dentro = sum(1 for a, b in bola.arestas if 2 in (a, b))
    assert dentro == 2, "a fixture precisa ter nó de borda com vizinho fora"


# ----------------------------------------- o corte é por nível inteiro


def test_nivel_que_nao_cabe_nao_entra_pela_metade(triangulo: tuple[Grafo, Any]) -> None:
    """Com teto 2, o nível 1 tem 2 nós e não cabe junto da origem.

    Nenhum deles entra: meio nível entregue seria subgrafo que parece completo.
    """
    grafo, _ = triangulo

    bola = vizinhanca(grafo, 0, saltos=1, teto_de_nos=2)

    assert bola.nos == (0,)
    assert bola.saltos == 0
    assert bola.truncada
    assert bola.nivel_recusado == 2, "diz o tamanho do que não está sendo visto"


def test_o_nivel_seguinte_e_recusado_inteiro(triangulo: tuple[Grafo, Any]) -> None:
    grafo, _ = triangulo

    bola = vizinhanca(grafo, 0, saltos=2, teto_de_nos=3)

    assert bola.nos == (0, 1, 2)
    assert bola.saltos == 1
    assert bola.truncada
    assert bola.nivel_recusado == 1
    assert 3 not in bola.nos, "nenhum nó do nível recusado aparece"


def test_teto_folgado_nao_trunca(triangulo: tuple[Grafo, Any]) -> None:
    """Controle positivo do corte: com espaço, tudo entra."""
    grafo, _ = triangulo

    bola = vizinhanca(grafo, 0, saltos=2, teto_de_nos=100)

    assert bola.nos == (0, 1, 2, 3)
    assert not bola.truncada
    assert bola.nivel_recusado == 0


def test_componente_esgotado_nao_e_truncamento(triangulo: tuple[Grafo, Any]) -> None:
    """A distinção que faria uma resposta inteira ser anunciada como parcial.

    Pedindo 10 saltos num componente de 5 nós, a resposta é completa: tudo o que
    existe até lá está ali. `saltos < saltos_pedidos` e `truncada` é falso.
    """
    grafo, _ = triangulo

    bola = vizinhanca(grafo, 0, saltos=10, teto_de_nos=100)

    assert bola.nos == (0, 1, 2, 3, 4)
    assert bola.saltos == 3
    assert bola.saltos_pedidos == 10
    assert not bola.truncada
    assert bola.nivel_recusado == 0


# --------------------------------------------- conferência e guardas


@pytest.mark.parametrize("semente", range(8))
def test_a_bola_confere_com_o_scipy(tmp_path: Path, semente: int) -> None:
    """O conjunto de nós é exatamente quem está a até k saltos, pelo scipy."""
    nos = 40
    grafo, _ = abrir(montar_csr(tmp_path, nos=nos, arestas=grafo_aleatorio(semente, nos, 55)))

    for origem in range(0, nos, 9):
        distancias = distancias_do_scipy(grafo, origem)
        for k in (1, 2, 3):
            bola = vizinhanca(grafo, origem, saltos=k, teto_de_nos=10_000)
            esperado = tuple(sorted(int(n) for n in np.flatnonzero(distancias <= k)))

            assert bola.nos == esperado
            assert dict(zip(bola.nos, bola.profundidades, strict=True)) == {
                int(n): int(distancias[n]) for n in esperado
            }


@pytest.mark.parametrize("semente", range(8))
def test_toda_aresta_induzida_existe_e_nenhuma_falta(tmp_path: Path, semente: int) -> None:
    """As duas metades da palavra "induzido": nada a mais, nada a menos."""
    nos = 40
    grafo, _ = abrir(montar_csr(tmp_path, nos=nos, arestas=grafo_aleatorio(semente, nos, 55)))

    bola = vizinhanca(grafo, 0, saltos=3, teto_de_nos=10_000)
    dentro = set(bola.nos)

    for a, b in bola.arestas:
        assert grafo.sao_vizinhos(a, b), "aresta que não existe no grafo"
    esperadas = {
        (a, int(b)) for a in bola.nos for b in grafo.vizinhos(a).tolist() if b > a and b in dentro
    }
    assert set(bola.arestas) == esperadas
    assert list(bola.arestas) == sorted(bola.arestas), "ordem total, para determinismo"


def test_a_vizinhanca_e_estavel_entre_chamadas(triangulo: tuple[Grafo, Any]) -> None:
    grafo, _ = triangulo

    resultados = {vizinhanca(grafo, 0, 2, 100) for _ in range(10)}

    assert len(resultados) == 1


@pytest.mark.parametrize(("saltos", "teto"), [(-1, 10), (2, 0), (2, -5)])
def test_pedido_invalido_e_recusado(triangulo: tuple[Grafo, Any], saltos: int, teto: int) -> None:
    grafo, _ = triangulo

    with pytest.raises(PedidoInvalidoError):
        vizinhanca(grafo, 0, saltos, teto)


def test_no_fora_da_faixa_na_vizinhanca(triangulo: tuple[Grafo, Any]) -> None:
    grafo, _ = triangulo

    with pytest.raises(NoForaDaFaixaError):
        vizinhanca(grafo, 99, 1, 100)


# --------------------------------- o orçamento de visitados, e os três desfechos


def test_orcamento_pequeno_faz_a_busca_desistir(corrente: tuple[Grafo, Any]) -> None:
    """Guarda que nunca reprovou não provou que sabe reprovar.

    O máximo real medido é de ~100 mil visitados, muito acima de qualquer
    orçamento que um teste possa gastar — então o disparo é forçado com um
    orçamento deliberadamente pequeno. Sem isto, o mecanismo estaria no código sem
    nunca ter sido exercitado.
    """
    grafo, componentes = corrente

    caminho = buscar_caminho(grafo, componentes, 0, 4, SEM_LIMITE, orcamento_de_visitados=3)

    assert caminho.desfecho is Desfecho.ORCAMENTO_EXCEDIDO
    assert caminho.nos == ()


def test_orcamento_folgado_encontra_o_mesmo_par(corrente: tuple[Grafo, Any]) -> None:
    """Controle positivo do outro lado: o par tem caminho, e com espaço ele aparece.

    É o que impede o teste anterior de passar por um motivo errado — um par sem
    caminho nenhum também devolveria "não encontrado".
    """
    grafo, componentes = corrente

    caminho = buscar_caminho(grafo, componentes, 0, 4, SEM_LIMITE, orcamento_de_visitados=10_000)

    assert caminho.encontrado
    assert caminho.nos == (0, 1, 2, 3, 4)


def test_o_orcamento_nao_altera_a_resposta_quando_sobra(corrente: tuple[Grafo, Any]) -> None:
    """O padrão e um orçamento absurdo devolvem o mesmo caminho."""
    grafo, componentes = corrente

    padrao = buscar_caminho(grafo, componentes, 0, 4, SEM_LIMITE)
    absurdo = buscar_caminho(grafo, componentes, 0, 4, SEM_LIMITE, orcamento_de_visitados=10**9)

    assert padrao.nos == absurdo.nos
    assert padrao.desfecho is absurdo.desfecho is Desfecho.ENCONTRADO


def test_orcamento_excedido_nao_e_ausencia_de_caminho(corrente: tuple[Grafo, Any]) -> None:
    """Os dois "não sei" e o único "não existe", sobre o mesmo grafo.

    O par (0, 4) tem caminho e devolve dois desfechos diferentes conforme o que
    faltou; o par (0, 5) não tem caminho nenhum. Colapsar os três em "não
    encontrado" apagaria a diferença entre "desisti", "não olhei tão fundo" e
    "não existe".
    """
    grafo, componentes = corrente

    desfechos = {
        buscar_caminho(grafo, componentes, 0, 4, SEM_LIMITE, 3).desfecho,
        buscar_caminho(grafo, componentes, 0, 4, 3).desfecho,
        buscar_caminho(grafo, componentes, 0, 5, SEM_LIMITE).desfecho,
        buscar_caminho(grafo, componentes, 0, 4, SEM_LIMITE).desfecho,
    }

    assert desfechos == {
        Desfecho.ORCAMENTO_EXCEDIDO,
        Desfecho.ALEM_DO_LIMITE,
        Desfecho.COMPONENTES_DIFERENTES,
        Desfecho.ENCONTRADO,
    }


def test_o_orcamento_e_conferido_antes_do_componente(corrente: tuple[Grafo, Any]) -> None:
    """Par sem caminho continua saindo pelo rótulo, sem gastar orçamento nenhum.

    A resposta O(1) não pode ser sabotada por um orçamento apertado: ela não
    percorre nada, então não há o que orçar.
    """
    grafo, componentes = corrente

    caminho = buscar_caminho(grafo, componentes, 0, 5, SEM_LIMITE, orcamento_de_visitados=1)

    assert caminho.desfecho is Desfecho.COMPONENTES_DIFERENTES
    assert caminho.visitados == 0


def test_o_padrao_fica_acima_do_pior_caso_medido() -> None:
    """A pior consulta medida no grafo real tocou 99.596 nós.

    Um padrão abaixo disso faria consultas legítimas — e já observadas — passarem
    a devolver ORCAMENTO_EXCEDIDO. O teste amarra a constante à medição que a
    justificou, para que baixá-la exija olhar o número.
    """
    assert ORCAMENTO_DE_VISITADOS >= 2 * PIOR_CASO_MEDIDO
