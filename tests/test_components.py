"""Componentes conexos, e a validação cruzada que confere a cadeia inteira.

O teste central deste arquivo não compara o `csgraph` com ele mesmo. Ele monta os
componentes por **union-find sobre `arestas.parquet`** — o insumo, antes de virar
CSR — e exige que a partição bata com a que o `csgraph` produziu **sobre o CSR**.

A diferença importa: comparar duas leituras do CSR validaria só o algoritmo de
componentes. Partindo do insumo, um erro na serialização também aparece, porque
os dois caminhos só concordam se o CSR descrever as mesmas arestas que entraram.
É o mesmo desenho do `csv.reader` contra o DuckDB na Fase 2.

As arestas são escritas à mão, e não geradas pela camada silver. Não é atalho: o
que decide se o cálculo de componentes está certo são **formas de grafo** —
gigante, empate de tamanho, par mútuo, nó que sobrou sem vizinho — e essas se
constroem diretamente, com o número esperado sabido de antemão.
"""

from __future__ import annotations

from collections import Counter
from pathlib import Path

import duckdb
import numpy as np
import pytest

from grafo_societario.config import Config
from grafo_societario.graph.build import serializar_csr
from grafo_societario.graph.components import (
    RotuloNaoCanonicoError,
    calcular_componentes,
    validar_rotulos_canonicos,
)
from grafo_societario.graph.csr import (
    ArtefatoAusenteError,
    ArtefatosIncompativeisError,
    abrir_grafo,
    carregar_componentes,
)

# ------------------------------------------- a implementação independente


class UnionFind:
    """Union-find com compressão de caminho e união por tamanho.

    Escrito aqui, e não importado do projeto, porque o ponto é ser outra
    implementação. Se ele chamasse o nosso código, a conferência compararia uma
    coisa com ela mesma — que é o defeito que a Fase 3 tinha em `qualidade.py` e
    que os dois refactors desta fase eliminaram.
    """

    def __init__(self, quantos: int) -> None:
        self.pai = list(range(quantos))
        self.tamanho = [1] * quantos

    def achar(self, no: int) -> int:
        raiz = no
        while self.pai[raiz] != raiz:
            raiz = self.pai[raiz]
        while self.pai[no] != raiz:
            self.pai[no], no = raiz, self.pai[no]
        return raiz

    def unir(self, a: int, b: int) -> None:
        a, b = self.achar(a), self.achar(b)
        if a == b:
            return
        if self.tamanho[a] < self.tamanho[b]:
            a, b = b, a
        self.pai[b] = a
        self.tamanho[a] += self.tamanho[b]

    def tamanhos(self) -> list[int]:
        """Tamanhos dos componentes, em ordem decrescente."""
        return sorted(Counter(self.achar(no) for no in range(len(self.pai))).values(), reverse=True)


def ler_pares(arestas: Path) -> list[tuple[int, int]]:
    with duckdb.connect() as conexao:
        return [
            (int(empresa), int(socio))
            for empresa, socio in conexao.execute(
                f"SELECT no_empresa, no_socio FROM read_parquet('{arestas.as_posix()}') "
                "WHERE no_empresa <> no_socio"
            ).fetchall()
        ]


def componentes_por_union_find(arestas: Path, nos: int) -> list[int]:
    """A partição vista a partir do insumo, sem passar pelo CSR.

    Laço é ignorado pela mesma razão pela qual a serialização o descarta: unir um
    nó a ele mesmo não é operação nenhuma. Nó que fica sem aresta continua
    existindo como componente de tamanho 1, que é o que o `csgraph` também produz.
    """
    conjunto = UnionFind(nos)
    for empresa, socio in ler_pares(arestas):
        conjunto.unir(empresa, socio)
    return conjunto.tamanhos()


# ------------------------------------------------- as formas de grafo


def montar(destino: Path, nos: int, arestas: list[tuple[int, int, str]]) -> Config:
    """Escreve `nos.parquet` e `arestas.parquet` como a fase os produz.

    `nos.parquet` sai com a coluna de identificador porque é o que ele tem; a
    serialização só lhe pergunta a contagem, e é a contagem que define quantas
    linhas o CSR vai ter.
    """
    grafo = destino / "grafo" / "2026-06"
    grafo.mkdir(parents=True, exist_ok=True)
    valores = ", ".join(f"({a}, {b}, '{q}')" for a, b, q in arestas)
    with duckdb.connect() as conexao:
        conexao.execute(
            f"COPY (SELECT * FROM (VALUES {valores}) "
            f"AS t(no_empresa, no_socio, qualificacao_socio)) "
            f"TO '{(grafo / 'arestas.parquet').as_posix()}' (FORMAT PARQUET)"
        )
        conexao.execute(
            f"COPY (SELECT format('{{:016d}}', i) AS identificador "
            f"FROM range({nos}) t(i)) "
            f"TO '{(grafo / 'nos.parquet').as_posix()}' (FORMAT PARQUET)"
        )
    return Config(competencia="2026-06", data_dir=destino, uf_alvo="SP")


VARIADO: list[tuple[int, int, str]] = [
    # Um componente que domina: 0-1-2-3-4-5.
    (0, 1, "49"),
    (1, 2, "49"),
    (2, 3, "49"),
    (3, 4, "49"),
    (4, 5, "49"),
    # Três componentes de tamanho 2, que empatam e precisam de desempate.
    (6, 7, "49"),
    (8, 9, "49"),
    # Par mútuo: A sócia de B e B sócia de A, com qualificações diferentes.
    (10, 11, "49"),
    (11, 10, "50"),
    # Laço: o nó 12 só se liga a si mesmo, e fica sem vizinho no CSR.
    (12, 12, "49"),
]
"""Treze nós cobrindo as quatro formas que decidem o cálculo.

Tamanhos esperados: 6, 2, 2, 2 e 1 — cinco componentes, gigante de 6, e um nó
sozinho que só tinha laço.
"""


@pytest.fixture
def variado(tmp_path: Path) -> Config:
    config = montar(tmp_path, nos=13, arestas=VARIADO)
    serializar_csr(config)
    return config


def arestas_de(config: Config) -> Path:
    return config.data_dir / "grafo" / "2026-06" / "arestas.parquet"


# ------------------------------------------------ a validação cruzada


def test_a_particao_confere_com_union_find_sobre_o_insumo(variado: Config) -> None:
    """Duas implementações independentes, partindo de lados diferentes da cadeia.

    O `csgraph` vê o CSR; o union-find vê `arestas.parquet`. Se a serialização
    tiver montado o grafo errado, os dois discordam — e é isso que esta
    conferência compra sobre uma que só trocasse de algoritmo.
    """
    resultado = calcular_componentes(variado)

    do_csr = sorted(Counter(int(r) for r in np.load(resultado.caminho)).values(), reverse=True)
    do_insumo = componentes_por_union_find(arestas_de(variado), resultado.nos)

    assert do_csr == do_insumo == [6, 2, 2, 2, 1]
    assert sum(do_csr) == resultado.nos
    assert len(do_csr) == resultado.quantos


def test_a_conferencia_sabe_discordar(variado: Config) -> None:
    """Controle positivo da validação cruzada.

    Comparação que nunca reprovou não provou que sabe reprovar. Com uma aresta a
    mais, ligando dois componentes que não se tocavam, o union-find tem de
    produzir outra partição — senão o teste acima passaria com os dois lados
    quebrados juntos.
    """
    original = componentes_por_union_find(arestas_de(variado), 13)

    conjunto = UnionFind(13)
    for empresa, socio in ler_pares(arestas_de(variado)):
        conjunto.unir(empresa, socio)
    conjunto.unir(0, 6)

    assert conjunto.tamanhos() != original
    assert conjunto.tamanhos() == [8, 2, 2, 1]


# ----------------------------------------- rótulo canônico, e não sorteio


def test_o_componente_zero_e_o_maior(variado: Config) -> None:
    """`componente 0` precisa querer dizer "o maior do grafo", e não "o primeiro
    que a varredura encontrou"."""
    resultado = calcular_componentes(variado)

    tamanhos = Counter(int(r) for r in np.load(resultado.caminho))
    assert tamanhos[0] == resultado.gigante == 6
    assert tamanhos[0] == max(tamanhos.values())


def test_os_tamanhos_sao_nao_crescentes(variado: Config) -> None:
    resultado = calcular_componentes(variado)

    tamanhos = Counter(int(r) for r in np.load(resultado.caminho))
    ordenados = [tamanhos[rotulo] for rotulo in sorted(tamanhos)]
    assert ordenados == [6, 2, 2, 2, 1]
    assert resultado.maiores == (6, 2, 2, 2, 1)


def test_empate_desempata_pelo_menor_indice_de_no(variado: Config) -> None:
    """Três componentes de tamanho 2: a ordem entre eles é pelo menor nó.

    Sem o desempate, ela ficaria a critério do algoritmo de ordenação — e o
    rótulo voltaria a ser sorteio justamente onde o tamanho não resolve.
    """
    resultado = calcular_componentes(variado)

    rotulos = [int(r) for r in np.load(resultado.caminho)]
    assert rotulos[6] == rotulos[7] == 1
    assert rotulos[8] == rotulos[9] == 2
    assert rotulos[10] == rotulos[11] == 3
    assert rotulos[12] == 4


@pytest.mark.parametrize(
    ("tamanhos", "defeito"),
    [([2, 5], "crescente"), ([5, 1, 3], "o menor no meio"), ([1, 9], "invertido")],
)
def test_rotulo_fora_da_ordem_de_tamanho_e_recusado(tamanhos: list[int], defeito: str) -> None:
    rotulos = np.repeat(np.arange(len(tamanhos), dtype=np.int32), tamanhos)

    with pytest.raises(RotuloNaoCanonicoError, match="tamanho decrescente"):
        validar_rotulos_canonicos(rotulos, np.array(tamanhos, dtype=np.int64))


def test_empate_fora_de_ordem_e_recusado() -> None:
    """O caso que a ordenação por tamanho sozinha não pega: dois componentes de
    mesmo tamanho com o de maior índice de nó rotulado primeiro."""
    rotulos = np.array([1, 1, 0, 0], dtype=np.int32)

    with pytest.raises(RotuloNaoCanonicoError, match="menor índice de nó"):
        validar_rotulos_canonicos(rotulos, np.array([2, 2], dtype=np.int64))


@pytest.mark.parametrize(
    ("rotulos", "tamanhos"),
    [([0, 0, 0, 1, 1, 2], [3, 2, 1]), ([0, 0, 1, 1], [2, 2]), ([], [])],
)
def test_rotulo_canonico_passa(rotulos: list[int], tamanhos: list[int]) -> None:
    """Controle positivo, inclusive com empate legítimo e com grafo vazio."""
    validar_rotulos_canonicos(np.array(rotulos, dtype=np.int32), np.array(tamanhos, dtype=np.int64))


# ------------------------------------------- laço, grau zero e cobertura


def test_no_que_so_tinha_laco_vira_componente_de_um(variado: Config) -> None:
    """Consequência da serialização, e não do grafo societário: o nó tem aresta na
    lista e nenhum vizinho depois do descarte do laço."""
    resultado = calcular_componentes(variado)

    assert resultado.isolados_no_csr == 1
    assert resultado.maiores[-1] == 1


def test_a_particao_cobre_todos_os_nos(variado: Config) -> None:
    """Nenhum nó fica sem componente."""
    resultado = calcular_componentes(variado)

    assert resultado.gigante + resultado.fora_do_gigante == resultado.nos == 13
    assert sum(resultado.maiores) == 13


def test_par_mutuo_nao_vira_dois_componentes(variado: Config) -> None:
    """O colapso do commit anterior, visto de outro ângulo."""
    resultado = calcular_componentes(variado)

    rotulos = np.load(resultado.caminho)
    assert int(rotulos[10]) == int(rotulos[11])


# --------------------------------------------- o que a Fase 5 vai consumir


def test_vizinho_esta_sempre_no_mesmo_componente(variado: Config) -> None:
    """A propriedade que define componente, conferida contra a topologia real."""
    calcular_componentes(variado)
    grafo = abrir_grafo(variado)

    componentes = carregar_componentes(variado, nos=grafo.nos)

    assert componentes.size == grafo.nos
    for no in range(grafo.nos):
        for vizinho in grafo.vizinhos(no):
            assert componentes[no] == componentes[int(vizinho)]


def test_componentes_diferentes_significam_ausencia_de_caminho(variado: Config) -> None:
    """A resposta negativa mais barata que existe: dois inteiros comparados, sem
    percorrer nada."""
    calcular_componentes(variado)
    grafo = abrir_grafo(variado)
    componentes = carregar_componentes(variado, nos=grafo.nos)

    assert componentes[0] != componentes[6], "o gigante não alcança o par"
    assert componentes[0] == componentes[5], "as duas pontas do gigante se alcançam"
    assert not grafo.sao_vizinhos(0, 6)


def test_carregar_componentes_e_mapeado_e_somente_leitura(variado: Config) -> None:
    """Mesma disciplina do CSR: mapear, não carregar."""
    calcular_componentes(variado)

    componentes = carregar_componentes(variado)

    assert componentes.dtype == np.int32
    assert componentes.flags.writeable is False


def test_componentes_de_outra_execucao_e_recusado(variado: Config) -> None:
    """Rótulos de tamanho diferente do conjunto de nós fariam cada índice devolver
    o componente de outro nó."""
    calcular_componentes(variado)

    with pytest.raises(ArtefatosIncompativeisError, match="execuções diferentes"):
        carregar_componentes(variado, nos=99)


def test_sem_componentes_a_mensagem_diz_onde_eles_nascem(variado: Config) -> None:
    with pytest.raises(ArtefatoAusenteError, match="construção do grafo"):
        carregar_componentes(variado)


# ------------------------------------------------------------ determinismo


def test_os_componentes_sao_deterministicos(variado: Config) -> None:
    primeiro = calcular_componentes(variado)
    antes = primeiro.caminho.read_bytes()

    segundo = calcular_componentes(variado)

    assert segundo.caminho.read_bytes() == antes


def test_o_tipo_do_rotulo_e_int32(variado: Config) -> None:
    resultado = calcular_componentes(variado)

    assert np.load(resultado.caminho).dtype == np.int32
