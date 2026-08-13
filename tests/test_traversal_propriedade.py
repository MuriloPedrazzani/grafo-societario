"""Os quatro desfechos da travessia, confrontados com oráculo independente.

Os testes de propriedade anteriores afirmavam sobre o caminho **quando ele
existe**. Isso deixava três desfechos de quatro sem confronto com verdade externa —
e são justamente os que afirmam ausência ou desistência, que é onde este projeto
não pode errar.

O mais caro deles é `ALEM_DO_LIMITE`. Ele diz "não procurei até lá"; se o caminho
estivesse dentro do limite pedido, a busca teria mentido, e nada no resultado
denunciaria isso. Só um oráculo pega — por isso a propriedade exige que o scipy
encontre caminho **estritamente maior** que o limite, e não apenas que encontre
algum.

## A fixture e o oráculo vêm da mesma biblioteca, de propósito

O CSR destes testes é montado por `coo_matrix(...).tocsr()`, e não pelo
serializador do projeto. Os rótulos de componente vêm do `connected_components`. O
oráculo de distância vem do `dijkstra`. Tudo scipy.

Assim o que está sob teste é **só a travessia**: um defeito no serializador não
pode mascarar um defeito na busca fazendo os dois errarem juntos. A equivalência
entre os dois construtores já foi estabelecida à parte, array a array, na
serialização em CSR.

## O que o Hypothesis acrescenta

Os testes semeados do commit 27 cobrem topologia aleatória. O que falta é a forma
que ninguém escolheria: cadeia pura, estrela, grafo completo, desconexo, nó único,
grafo sem aresta nenhuma, e multiaresta antes do colapso. Elas entram como formas
declaradas, misturadas à geração livre.

E o valor principal não é o volume de casos: é a **redução**. Quando uma
propriedade quebra, o contraexemplo que aparece no relatório é o menor grafo que
ainda quebra — que é a diferença entre um defeito investigável e um grafo de
quarenta nós para ler à mão.
"""

from __future__ import annotations

from typing import Any

import numpy as np
import pytest
from hypothesis import given, settings
from hypothesis import strategies as st
from scipy.sparse import coo_matrix
from scipy.sparse.csgraph import connected_components, dijkstra

from grafo_societario.graph.traversal import Caminho, Desfecho, buscar_caminho

FOLGADO = 10_000
"""Limite e orçamento altos o bastante para nunca cortar nos grafos gerados."""

MAXIMO_DE_NOS = 24
MAXIMO_COMPLETO = 7
"""O grafo completo cresce ao quadrado; acima disso o caso deixa de ser mínimo."""


class GrafoDeTeste:
    """CSR montado inteiramente pelo scipy, com a interface que a travessia usa.

    Não herda de `Grafo` nem passa pelo `abrir_grafo`: o ponto é que nada do
    serializador do projeto participe da construção. A interface é a que a busca
    consome — `grau`, `vizinhos`, `nos`.
    """

    def __init__(self, nos: int, arestas: list[tuple[int, int]]) -> None:
        pares = [(a, b) for a, b in arestas if a != b]
        linhas = np.array([a for a, _ in pares] + [b for _, b in pares], dtype=np.int32)
        colunas = np.array([b for _, b in pares] + [a for a, _ in pares], dtype=np.int32)
        # `tocsr` funde duplicados e ordena a linha: é ele que colapsa a
        # multiaresta que o gerador produziu, e o resultado é o grafo simples.
        self.matriz = coo_matrix(
            (np.ones(linhas.size, dtype=np.int8), (linhas, colunas)),
            shape=(nos, nos),
        ).tocsr()
        self.indptr = self.matriz.indptr
        self.indices = self.matriz.indices
        self.nos = nos
        _, rotulos = connected_components(self.matriz, directed=False, return_labels=True)
        self.componentes: Any = rotulos.astype(np.int32)

    def grau(self, no: int) -> int:
        if not 0 <= no < self.nos:
            raise IndexError(no)
        return int(self.indptr[no + 1]) - int(self.indptr[no])

    def vizinhos(self, no: int) -> Any:
        return self.indices[self.indptr[no] : self.indptr[no + 1]]

    def distancias(self, origem: int) -> Any:
        saida: Any = dijkstra(self.matriz, directed=False, unweighted=True, indices=[origem])
        return saida[0]


FORMAS = (
    "livre",
    "cadeia",
    "estrela",
    "completo",
    "desconexo",
    "sem_arestas",
    "multiaresta",
)


def arestas_da_forma(
    forma: str, nos: int, livres: list[tuple[int, int]], repeticoes: int
) -> tuple[int, list[tuple[int, int]]]:
    """A construção de cada forma, separada da geração para poder ser testada.

    Devolve `(nos, arestas)` porque o grafo completo reduz o número de nós: ele
    cresce ao quadrado, e acima de um punhado deixa de ser contraexemplo mínimo.
    """
    if forma == "cadeia":
        return nos, [(i, i + 1) for i in range(nos - 1)]
    if forma == "estrela":
        return nos, [(0, i) for i in range(1, nos)]
    if forma == "completo":
        tamanho = min(nos, MAXIMO_COMPLETO)
        return tamanho, [(i, j) for i in range(tamanho) for j in range(i + 1, tamanho)]
    if forma == "desconexo":
        meio = nos // 2
        return nos, [(i, i + 1) for i in range(meio - 1)] + [
            (i, i + 1) for i in range(meio, nos - 1)
        ]
    if forma == "sem_arestas":
        return nos, []
    if forma == "multiaresta":
        return nos, livres * repeticoes
    return nos, livres


@st.composite
def rede(desenhar: st.DrawFn) -> GrafoDeTeste:
    """Formas que ninguém escreveria à mão, mais geração livre."""
    forma = desenhar(st.sampled_from(FORMAS))
    nos = desenhar(st.integers(min_value=1, max_value=MAXIMO_DE_NOS))
    no = st.integers(min_value=0, max_value=nos - 1)
    livres = desenhar(st.lists(st.tuples(no, no), max_size=40))
    repeticoes = desenhar(st.integers(min_value=2, max_value=3))

    tamanho, arestas = arestas_da_forma(forma, nos, livres, repeticoes)
    return GrafoDeTeste(tamanho, arestas)


def caminho_e_valido(grafo: GrafoDeTeste, caminho: Caminho) -> bool:
    return all(
        int(b) in {int(v) for v in grafo.vizinhos(int(a))}
        for a, b in zip(caminho.nos, caminho.nos[1:], strict=False)
    )


@given(
    grafo=rede(),
    par=st.tuples(st.integers(min_value=0), st.integers(min_value=0)),
    limite=st.integers(min_value=0, max_value=12),
    orcamento=st.integers(min_value=1, max_value=60),
)
@settings(max_examples=400)
def test_os_quatro_desfechos_conferem_com_o_scipy(
    grafo: GrafoDeTeste,
    par: tuple[int, int],
    limite: int,
    orcamento: int,
) -> None:
    """Cada desfecho é confrontado com o que o scipy sabe do mesmo grafo.

    Nenhum deles passa por não ter sido examinado: os três que negam ou desistem
    têm afirmação própria, e a de `ALEM_DO_LIMITE` é estrita.
    """
    origem, destino = par[0] % grafo.nos, par[1] % grafo.nos
    grafo_typed: Any = grafo

    resultado = buscar_caminho(grafo_typed, grafo.componentes, origem, destino, limite, orcamento)
    distancia = grafo.distancias(origem)[destino]

    if resultado.desfecho is Desfecho.ENCONTRADO:
        assert not np.isinf(distancia), "achou caminho onde o scipy diz não haver"
        assert resultado.saltos == int(distancia), "não é o caminho mínimo"
        assert resultado.saltos <= limite, "passou do limite pedido"
        assert caminho_e_valido(grafo, resultado), "aresta inexistente no caminho"
        assert resultado.nos[0] == origem
        assert resultado.nos[-1] == destino

        volta = buscar_caminho(grafo_typed, grafo.componentes, destino, origem, limite, orcamento)
        assert volta.nos == tuple(reversed(resultado.nos)), "não é o reverso exato"

    elif resultado.desfecho is Desfecho.COMPONENTES_DIFERENTES:
        assert np.isinf(distancia), "negou caminho que o scipy encontra"
        assert resultado.visitados == 0, "a negativa não percorre nada"

    elif resultado.desfecho is Desfecho.ALEM_DO_LIMITE:
        # A afirmação mais importante do arquivo. Se o scipy achar caminho dentro
        # do limite, a busca disse "não procurei" sobre algo que estava ao alcance.
        assert not np.isinf(distancia), "disse 'além do limite' sobre par sem caminho"
        assert int(distancia) > limite, (
            f"o caminho tem {int(distancia)} saltos e o limite era {limite}: "
            "estava ao alcance e a busca afirmou não ter procurado"
        )

    else:
        assert resultado.desfecho is Desfecho.ORCAMENTO_EXCEDIDO
        # Desistir não é negar: o componente garante que o caminho existe. Note
        # que COMPONENTES_DIFERENTES é impossível aqui — este desfecho só nasce
        # depois de o portão de componente ter passado.
        assert not np.isinf(distancia), "desistiu de par que não tinha caminho nenhum"

        com_folga = buscar_caminho(grafo_typed, grafo.componentes, origem, destino, limite, FOLGADO)
        assert com_folga.desfecho is not Desfecho.COMPONENTES_DIFERENTES

        tudo_folgado = buscar_caminho(
            grafo_typed, grafo.componentes, origem, destino, FOLGADO, FOLGADO
        )
        assert tudo_folgado.desfecho is Desfecho.ENCONTRADO, (
            "com orçamento e limite folgados o caminho tem de aparecer: "
            "a desistência era 'não sei', não 'não existe'"
        )
        assert tudo_folgado.saltos == int(distancia)


@given(grafo=rede(), par=st.tuples(st.integers(min_value=0), st.integers(min_value=0)))
@settings(max_examples=200)
def test_o_caminho_e_sempre_o_reverso_no_sentido_contrario(
    grafo: GrafoDeTeste, par: tuple[int, int]
) -> None:
    """A canonização do par, sobre formas que a fixture escrita à mão não produz."""
    origem, destino = par[0] % grafo.nos, par[1] % grafo.nos
    grafo_typed: Any = grafo

    ida = buscar_caminho(grafo_typed, grafo.componentes, origem, destino, FOLGADO, FOLGADO)
    volta = buscar_caminho(grafo_typed, grafo.componentes, destino, origem, FOLGADO, FOLGADO)

    assert volta.desfecho is ida.desfecho
    assert volta.nos == tuple(reversed(ida.nos))


@given(
    grafo=rede(),
    par=st.tuples(st.integers(min_value=0), st.integers(min_value=0)),
    limite=st.integers(min_value=0, max_value=12),
)
@settings(max_examples=200)
def test_o_limite_nunca_e_ultrapassado(
    grafo: GrafoDeTeste, par: tuple[int, int], limite: int
) -> None:
    origem, destino = par[0] % grafo.nos, par[1] % grafo.nos
    grafo_typed: Any = grafo

    resultado = buscar_caminho(grafo_typed, grafo.componentes, origem, destino, limite, FOLGADO)

    assert resultado.saltos <= limite


@given(grafo=rede(), no=st.integers(min_value=0))
@settings(max_examples=100)
def test_o_no_alcanca_a_si_mesmo_em_zero_saltos(grafo: GrafoDeTeste, no: int) -> None:
    """Vale inclusive no grafo de um nó só e no grafo sem aresta nenhuma."""
    alvo = no % grafo.nos
    grafo_typed: Any = grafo

    resultado = buscar_caminho(grafo_typed, grafo.componentes, alvo, alvo, 0, 1)

    assert resultado.desfecho is Desfecho.ENCONTRADO
    assert resultado.nos == (alvo,)
    assert resultado.saltos == 0


@pytest.mark.parametrize("forma", FORMAS)
def test_cada_forma_declarada_produz_grafo_valido(forma: str) -> None:
    """Controle positivo do gerador, forma a forma.

    Uma estratégia que nunca produzisse a forma declarada faria as propriedades
    passarem sem nunca terem visto o caso, e nada no resultado diria isso. Aqui
    cada uma é construída diretamente e conferida.
    """
    tamanho, arestas = arestas_da_forma(forma, nos=8, livres=[(0, 1), (1, 2)], repeticoes=3)

    grafo = GrafoDeTeste(tamanho, arestas)

    assert grafo.nos >= 1
    assert all(0 <= a < tamanho and 0 <= b < tamanho for a, b in arestas)


def test_a_forma_completa_liga_todos_com_todos() -> None:
    tamanho, arestas = arestas_da_forma("completo", nos=99, livres=[], repeticoes=2)

    grafo = GrafoDeTeste(tamanho, arestas)

    assert tamanho == MAXIMO_COMPLETO
    assert all(grafo.grau(no) == tamanho - 1 for no in range(tamanho))


def test_a_forma_desconexa_tem_mais_de_um_componente() -> None:
    tamanho, arestas = arestas_da_forma("desconexo", nos=8, livres=[], repeticoes=2)

    grafo = GrafoDeTeste(tamanho, arestas)

    assert len(set(int(r) for r in grafo.componentes)) > 1


def test_a_multiaresta_colapsa_na_montagem() -> None:
    """O gerador produz a repetida; o `tocsr` funde. É o que o CSR real faz."""
    tamanho, arestas = arestas_da_forma(
        "multiaresta", nos=4, livres=[(0, 1), (0, 1), (1, 2)], repeticoes=3
    )

    grafo = GrafoDeTeste(tamanho, arestas)

    assert len(arestas) == 9, "a lista de entrada tem repetição"
    assert grafo.grau(0) == 1, "e o grafo montado não"
    assert [int(v) for v in grafo.vizinhos(1)] == [0, 2]


def test_a_forma_sem_arestas_nao_quebra_a_montagem() -> None:
    """Grafo vazio é caso de borda de tudo: `bincount`, `coo_matrix`, travessia."""
    tamanho, arestas = arestas_da_forma("sem_arestas", nos=1, livres=[], repeticoes=2)

    grafo = GrafoDeTeste(tamanho, arestas)

    assert arestas == []
    assert grafo.grau(0) == 0


def test_o_gerador_produz_os_quatro_desfechos() -> None:
    """Controle positivo da propriedade inteira, e o mais importante do arquivo.

    Uma propriedade que examina quatro ramos mas só vê um passa verde para
    sempre, e o verde não significa nada nos outros três. Isto varre as formas
    declaradas contra uma grade de pares, limites e orçamentos, e exige que os
    quatro desfechos apareçam.

    A varredura é fixa, e não gerada: o que ela protege é justamente o gerador, e
    um controle que dependesse dele não controlaria nada.
    """
    vistos: dict[Desfecho, int] = {}
    for forma in FORMAS:
        tamanho, arestas = arestas_da_forma(
            forma, nos=12, livres=[(0, 1), (1, 2), (2, 3), (5, 6), (8, 9)], repeticoes=2
        )
        grafo = GrafoDeTeste(tamanho, arestas)
        grafo_typed: Any = grafo
        for origem in range(0, tamanho, 3):
            for destino in range(0, tamanho, 2):
                for limite, orcamento in ((1, 3), (2, 4), (12, 3), (12, FOLGADO)):
                    desfecho = buscar_caminho(
                        grafo_typed, grafo.componentes, origem, destino, limite, orcamento
                    ).desfecho
                    vistos[desfecho] = vistos.get(desfecho, 0) + 1

    assert set(vistos) == set(Desfecho), (
        f"a varredura não produziu todos os desfechos: só viu {sorted(d.value for d in vistos)}. "
        "Uma propriedade que examina quatro ramos e vê menos passa verde sem significar nada."
    )
