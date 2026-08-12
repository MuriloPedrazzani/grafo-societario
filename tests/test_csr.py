"""Leitura do CSR por `mmap`: vista em vez de cópia, e memória medida de verdade.

Dois testes aqui não afirmam resultado, e sim **como** ele foi obtido. Isso é
deliberado: uma implementação que carregasse os três arrays inteiros passaria em
todo teste de valor deste arquivo. O que distingue mapear de carregar não aparece
no que se lê — aparece em quanto se pagou para ler.
"""

from __future__ import annotations

import gc
from pathlib import Path
from typing import Any, Final

import numpy as np
import psutil
import pytest

from grafo_societario.config import Config
from grafo_societario.graph.csr import (
    ArtefatoAusenteError,
    ArtefatosIncompativeisError,
    Grafo,
    NoForaDaFaixaError,
    abrir_grafo,
)

MIB = 1024 * 1024


def gravar_csr(
    destino: Path,
    indptr: list[int],
    indices: list[int],
    qualificacoes: list[int],
) -> None:
    destino.mkdir(parents=True, exist_ok=True)
    for nome, valores, tipo in (
        ("indptr.npy", indptr, np.int32),
        ("indices.npy", indices, np.int32),
        ("qualificacoes.npy", qualificacoes, np.int8),
    ):
        with (destino / nome).open("wb") as arquivo:
            np.save(arquivo, np.array(valores, dtype=tipo), allow_pickle=False)


@pytest.fixture
def grafo(tmp_path: Path) -> Grafo:
    """Quatro nós: um triângulo entre 0, 1 e 2, e o nó 3 isolado.

    O isolado existe porque a linha vazia é o caso que confunde `indptr`: ela
    precisa existir e ter comprimento zero, senão a posição deixa de ser o índice.
    """
    gravar_csr(
        tmp_path / "grafo" / "2026-06",
        indptr=[0, 2, 4, 6, 6],
        indices=[1, 2, 0, 2, 0, 1],
        qualificacoes=[10, 20, 10, 30, 20, 30],
    )
    return abrir_grafo(Config(competencia="2026-06", data_dir=tmp_path, uf_alvo="SP"))


# --------------------------------------------- o que se lê


def test_vizinhos_de_cada_no(grafo: Grafo) -> None:
    assert [int(v) for v in grafo.vizinhos(0)] == [1, 2]
    assert [int(v) for v in grafo.vizinhos(2)] == [0, 1]
    assert [int(v) for v in grafo.vizinhos(3)] == []


def test_grau_le_dois_inteiros(grafo: Grafo) -> None:
    assert [grafo.grau(no) for no in range(grafo.nos)] == [2, 2, 2, 0]


def test_qualificacao_acompanha_o_vizinho_posicao_a_posicao(grafo: Grafo) -> None:
    """O alinhamento que a serialização garante, exercitado pelo lado de quem lê."""
    vizinhos = [int(v) for v in grafo.vizinhos(0)]
    qualificacoes = [int(q) for q in grafo.qualificacoes_de(0)]

    assert dict(zip(vizinhos, qualificacoes, strict=True)) == {1: 10, 2: 20}


def test_adjacencia_e_simetrica(grafo: Grafo) -> None:
    assert grafo.sao_vizinhos(0, 1)
    assert grafo.sao_vizinhos(1, 0)
    assert not grafo.sao_vizinhos(0, 3)
    assert not grafo.sao_vizinhos(3, 0)


def test_no_isolado_responde_sem_vizinho_e_nao_erro(grafo: Grafo) -> None:
    """Existe e não tem vínculo — a distinção que a fase inteira preserva."""
    assert grafo.grau(3) == 0
    assert grafo.vizinhos(3).size == 0


def test_tamanhos_declarados(grafo: Grafo) -> None:
    assert grafo.nos == 4
    assert grafo.posicoes == 6


# ------------------------------------- vista, e não cópia


def test_vizinhos_compartilha_memoria_com_o_mapeamento(grafo: Grafo) -> None:
    """A afirmação que distingue mapear de carregar.

    Valores certos uma cópia também tem. O que prova que não houve cópia é o
    objeto devolvido compartilhar memória com o arquivo mapeado.
    """
    linha = grafo.vizinhos(0)

    assert np.shares_memory(linha, grafo.indices)
    assert linha.base is not None


def test_qualificacoes_tambem_e_vista(grafo: Grafo) -> None:
    assert np.shares_memory(grafo.qualificacoes_de(1), grafo.qualificacoes)


def test_o_mapeamento_e_somente_leitura(grafo: Grafo) -> None:
    """Artefato imutável aberto para escrita transforma defeito de código em
    corrupção do arquivo publicado."""
    assert grafo.indices.flags.writeable is False

    with pytest.raises(ValueError, match=r"read-only|assignment"):
        grafo.vizinhos(0)[0] = 99


@pytest.mark.parametrize(
    ("materializar", "operacao"),
    [
        (lambda linha: np.asarray(list(linha)), "list"),
        (np.sort, "np.sort"),
        (lambda linha: linha + 0, "aritmética"),
        (np.copy, "copy"),
    ],
)
def test_operacao_comum_desfaz_a_vista(grafo: Grafo, materializar: Any, operacao: str) -> None:
    """Controle negativo, e o aviso que o consumidor precisa ter visto.

    Perder a vista não produz erro nem valor diferente: produz o mesmo resultado
    com o `mmap` virando custo morto. Se um dia alguma destas passar a
    compartilhar memória, é a premissa deste módulo que mudou.
    """
    assert not np.shares_memory(materializar(grafo.vizinhos(0)), grafo.indices), operacao


# ------------------------------------- o medidor, antes de medir


def residente() -> int:
    """Memória residente do processo, em bytes."""
    return int(psutil.Process().memory_info().rss)


def test_o_medidor_de_memoria_detecta_alocacao_real() -> None:
    """Controle positivo do instrumento, e ele vem antes de qualquer medição.

    Um medidor que só sabe devolver zero provaria "não houve alocação" para
    qualquer implementação, inclusive uma que carregasse tudo. Este projeto já foi
    mordido por instrumento que reportava zero com o código certo — desde então,
    medidor prova que sabe medir antes de a medição valer.
    """
    antes = residente()

    bloco = np.ones(64 * MIB, dtype=np.int8)
    bloco[::4096] = 7  # toca uma página em cada, para o sistema materializar
    crescimento = residente() - antes

    assert crescimento >= 48 * MIB, (
        f"o medidor viu {crescimento / MIB:.1f} MiB para uma alocação de 64 MiB; "
        "sem detectar isto, ele não pode provar ausência de alocação"
    )
    del bloco
    gc.collect()


@pytest.fixture
def grafo_grande(tmp_path: Path) -> Grafo:
    """Um CSR de 32 MiB em `indices`: grande o bastante para a diferença entre
    mapear e carregar ficar acima do ruído de medição."""
    nos = 1024
    por_no = 8192
    posicoes = nos * por_no
    indptr = np.arange(nos + 1, dtype=np.int32) * por_no
    indices = np.tile(np.arange(por_no, dtype=np.int32), nos)
    qualificacoes = np.zeros(posicoes, dtype=np.int8)

    destino = tmp_path / "grafo" / "2026-06"
    destino.mkdir(parents=True)
    for nome, array in (
        ("indptr.npy", indptr),
        ("indices.npy", indices),
        ("qualificacoes.npy", qualificacoes),
    ):
        with (destino / nome).open("wb") as arquivo:
            np.save(arquivo, array, allow_pickle=False)

    del indptr, indices, qualificacoes
    gc.collect()
    return abrir_grafo(Config(competencia="2026-06", data_dir=tmp_path, uf_alvo="SP"))


def test_abrir_nao_carrega_o_arquivo(grafo_grande: Grafo) -> None:
    """O primeiro dos dois momentos: abrir é mapear, não desserializar."""
    gc.collect()
    antes = residente()

    linha = grafo_grande.vizinhos(0)

    crescimento = residente() - antes
    assert grafo_grande.posicoes * 4 >= 32 * MIB, "o arquivo precisa ser grande para valer"
    assert crescimento < 4 * MIB, (
        f"abrir e ler uma linha custou {crescimento / MIB:.1f} MiB sobre um indices de "
        f"{grafo_grande.posicoes * 4 / MIB:.0f} MiB"
    )
    assert linha.size == 8192


TOQUES: Final = 32
"""Linhas tocadas no teste de paginação sob demanda.

O número é baixo de propósito. O Linux lê adiante até 128 KiB por falta de
página, então tocar muitas linhas de um arquivo de 32 MiB pode trazer o arquivo
inteiro — e o teste passaria a medir a política de readahead do sistema em vez do
comportamento deste módulo. Com 32 toques, o pior caso de readahead é 4 MiB, bem
dentro do teto de 8 MiB que se afirma aqui.

A medição interessante, com acesso denso sobre o artefato real, está no corpo do
commit e não neste teste: aqui o que precisa ser verdade em qualquer máquina é
que o custo acompanha o que foi tocado, e não o tamanho do arquivo.
"""


def test_acesso_aleatorio_pagina_sob_demanda(grafo_grande: Grafo) -> None:
    """O segundo momento, e o que interessa.

    `mmap` não é "nunca carrega", é "carrega sob demanda": a memória residente
    acompanha o que foi tocado. Tocar 32 das 1.024 linhas traz as páginas
    correspondentes, e o crescimento fica muito abaixo dos 32 MiB do arquivo — o
    que se afirma é a proporcionalidade, não que o custo seja zero.
    """
    gc.collect()
    antes = residente()

    aleatorio = np.random.default_rng(42)
    for no in aleatorio.permutation(grafo_grande.nos)[:TOQUES]:
        int(grafo_grande.vizinhos(int(no))[0])

    crescimento = residente() - antes
    inteiro = grafo_grande.posicoes * 4
    assert crescimento < inteiro // 4, (
        f"tocar {TOQUES} linhas custou {crescimento / MIB:.1f} MiB de um arquivo de "
        f"{inteiro / MIB:.0f} MiB — não está paginando sob demanda"
    )


def test_varrer_tudo_carrega_quase_tudo(grafo_grande: Grafo) -> None:
    """O contraponto honesto: a economia é do acesso esparso, não do formato.

    Somar todas as posições toca todas as páginas, e a memória residente sobe
    para perto do arquivo inteiro. Sem este teste, os dois anteriores dariam a
    impressão de que `mmap` torna o arquivo gratuito.
    """
    gc.collect()
    antes = residente()

    total = 0
    for no in range(grafo_grande.nos):
        total += int(grafo_grande.vizinhos(no).sum())

    crescimento = residente() - antes
    assert total > 0
    assert crescimento > 8 * MIB, (
        f"varrer o arquivo inteiro cresceu só {crescimento / MIB:.1f} MiB; a medição não está "
        "vendo o que deveria"
    )


# ------------------------------------- as guardas de abertura e de faixa


@pytest.mark.parametrize("no", [-1, 4, 100, -100])
def test_no_fora_da_faixa_e_recusado(grafo: Grafo, no: int) -> None:
    """Índice negativo é válido para o NumPy: `vizinhos(-1)` devolveria fatia
    vazia e se leria como "sem vizinhos"."""
    with pytest.raises(NoForaDaFaixaError, match="fora da faixa"):
        grafo.vizinhos(no)


def test_grau_tambem_confere_a_faixa(grafo: Grafo) -> None:
    with pytest.raises(NoForaDaFaixaError):
        grafo.grau(-1)


@pytest.mark.parametrize("ausente", ["indptr.npy", "indices.npy", "qualificacoes.npy"])
def test_array_ausente_diz_qual_falta(tmp_path: Path, ausente: str) -> None:
    destino = tmp_path / "grafo" / "2026-06"
    gravar_csr(destino, [0, 2], [1, 0], [10, 10])
    (destino / ausente).unlink()

    with pytest.raises(ArtefatoAusenteError, match=ausente):
        abrir_grafo(Config(competencia="2026-06", data_dir=tmp_path, uf_alvo="SP"))


def test_indices_de_outra_execucao_e_recusado(tmp_path: Path) -> None:
    """O acidente mais fácil com três arquivos soltos, e o mais difícil de notar:
    a consulta responde normalmente, apontando para o nó errado."""
    gravar_csr(tmp_path / "grafo" / "2026-06", [0, 2, 4], [1, 0], [10, 10])

    with pytest.raises(ArtefatosIncompativeisError, match="execuções diferentes"):
        abrir_grafo(Config(competencia="2026-06", data_dir=tmp_path, uf_alvo="SP"))


def test_atributo_de_tamanho_diferente_e_recusado(tmp_path: Path) -> None:
    gravar_csr(tmp_path / "grafo" / "2026-06", [0, 2], [1, 0], [10])

    with pytest.raises(ArtefatosIncompativeisError, match="paralelo a indices"):
        abrir_grafo(Config(competencia="2026-06", data_dir=tmp_path, uf_alvo="SP"))


def test_grafo_valido_abre(tmp_path: Path) -> None:
    """Controle positivo das guardas de abertura."""
    gravar_csr(tmp_path / "grafo" / "2026-06", [0, 1, 2], [1, 0], [10, 10])

    grafo = abrir_grafo(Config(competencia="2026-06", data_dir=tmp_path, uf_alvo="SP"))

    assert grafo.nos == 2
    assert grafo.competencia == "2026-06"
