"""O benchmark, e o amostrador de memória que precisa provar que sabe medir.

O teste central deste arquivo não confere um número medido — confere que o
instrumento **consegue** medir. Um amostrador com intervalo grande demais, ou
preso atrás do GIL, devolveria a linha de base, e o relatório sairia com um pico
excelente e falso.
"""

from __future__ import annotations

import datetime as dt
import time
from pathlib import Path

import numpy as np
import pytest

from grafo_societario.config import Config
from grafo_societario.graph.benchmark import (
    MIB,
    TETO_DE_DEPLOY,
    Amostrador,
    Benchmark,
    escrever_relatorio,
    medir,
    medir_construcao,
)
from grafo_societario.graph.build import serializar_csr
from grafo_societario.graph.components import calcular_componentes
from test_components import VARIADO, montar

MEDIDO_EM = dt.date(2026, 8, 12)


def benchmark_de_teste(
    etapas: tuple[object, ...] = (),
    artefatos: tuple[tuple[str, int], ...] = (("nos.parquet", 1000),),
) -> Benchmark:
    return Benchmark(
        competencia="2026-06",
        uf_alvo="SP",
        medido_em=MEDIDO_EM,
        etapas=etapas,  # type: ignore[arg-type]
        artefatos=artefatos,
        nucleos=8,
        memoria_da_maquina=8 * 1024**3,
    )


# ------------------------------------------- o instrumento, antes da medição


def test_o_amostrador_ve_alocacao_transitoria() -> None:
    """Controle positivo, e o motivo de o amostrador existir como classe.

    A alocação nasce e morre **dentro** do bloco medido. Um medidor que só olhasse
    a residente no fim não veria nada, porque nesse momento a memória já foi
    devolvida — e é exatamente esse o erro que este teste impede.
    """
    with Amostrador(intervalo=0.01) as amostrador:
        base = amostrador.pico
        bloco = np.ones(96 * MIB, dtype=np.int8)
        bloco[::4096] = 7
        time.sleep(0.1)
        del bloco

    assert amostrador.pico - base >= 64 * MIB, (
        f"o amostrador viu {(amostrador.pico - base) / MIB:.1f} MiB de uma alocação de 96 MiB; "
        "sem enxergar isto, o pico que ele reporta não significa nada"
    )


def test_o_amostrador_nao_inventa_pico() -> None:
    """Controle negativo: sem alocação, o pico fica junto da linha de base.

    Sem ele, o teste anterior seria satisfeito por um medidor que devolvesse um
    número grande sempre.
    """
    with Amostrador(intervalo=0.01) as amostrador:
        base = amostrador.pico
        time.sleep(0.1)

    assert amostrador.pico - base < 32 * MIB


def test_medir_cronometra_e_devolve_o_resultado() -> None:
    etapa = medir("etapa de teste", lambda: "produziu alguma coisa")

    assert etapa.nome == "etapa de teste"
    assert etapa.segundos >= 0
    assert etapa.resultado == "produziu alguma coisa"
    assert etapa.pico >= etapa.residente_inicial


def test_acrescimo_nunca_e_negativo() -> None:
    """Etapa que coube no que as anteriores já reservaram custou zero, e não menos
    que zero — residente que encolhe é a anterior devolvendo, não esta poupando."""
    assert medir("etapa barata", lambda: "nada").acrescimo >= 0


# ------------------------------------------------- a construção medida


def test_as_etapas_reais_sao_medidas(tmp_path: Path) -> None:
    """Cada etapa da fase é cronometrada e descreve o que produziu."""
    config = montar(tmp_path, nos=13, arestas=VARIADO)

    csr = medir("serialização em CSR", lambda: str(serializar_csr(config).arestas))
    componentes = medir("componentes conexos", lambda: str(calcular_componentes(config).quantos))

    assert csr.resultado == "8"
    assert componentes.resultado == "5"
    assert csr.pico > 0
    assert componentes.pico > 0


def test_construcao_completa_exige_o_silver(tmp_path: Path) -> None:
    """A medição roda a fase de verdade, e a fase de verdade parte do silver."""
    config = Config(competencia="2026-06", data_dir=tmp_path, uf_alvo="SP")

    with pytest.raises(RuntimeError):
        medir_construcao(config)


def test_a_margem_de_deploy_sai_do_que_foi_medido() -> None:
    benchmark = benchmark_de_teste(artefatos=(("nos.parquet", TETO_DE_DEPLOY // 2),))

    assert benchmark.bytes_publicaveis == TETO_DE_DEPLOY // 2
    assert abs(benchmark.margem_de_deploy - 0.5) < 0.001


# ------------------------------------------------------------ o relatório


def test_o_relatorio_traz_etapas_e_artefatos(tmp_path: Path) -> None:
    destino = tmp_path / "benchmark.md"
    benchmark = benchmark_de_teste(
        etapas=(medir("nós com índice denso", lambda: "13 nós"),),
        artefatos=(("indptr.npy", 1000), ("indices.npy", 2000)),
    )

    escrever_relatorio(benchmark, destino)

    texto = destino.read_text(encoding="utf-8")
    assert "nós com índice denso" in texto
    assert "`indptr.npy`" in texto
    assert "2026-06" in texto
    assert "2026-08-12" in texto


def test_o_relatorio_nao_promete_o_que_nao_mediu(tmp_path: Path) -> None:
    """A comparação com banco de grafo gerenciado vem com a ressalva de
    verificação: número de fornecedor envelhece, e este documento vira post."""
    destino = tmp_path / "benchmark.md"

    escrever_relatorio(benchmark_de_teste(), destino)

    texto = destino.read_text(encoding="utf-8")
    assert "sujeitos a mudança" in texto
    assert "comparação honesta" in texto.lower()
    assert "escrita concorrente" in texto, "a ressalva que inverte a conclusão"


def test_o_relatorio_declara_a_divergencia_da_fonte(tmp_path: Path) -> None:
    """As próprias fontes do fornecedor discordam do teto do plano gratuito, e o
    documento usa o maior — esconder a divergência seria escolher o número que
    favorece o argumento."""
    destino = tmp_path / "benchmark.md"

    escrever_relatorio(benchmark_de_teste(), destino)

    texto = destino.read_text(encoding="utf-8")
    assert "divergem entre si" in texto
    assert "50.000" in texto and "200.000" in texto


def test_as_tabelas_do_relatorio_tem_colunas_consistentes(tmp_path: Path) -> None:
    """Tabela markdown com número de colunas variável renderiza torta e silencia."""
    destino = tmp_path / "benchmark.md"
    benchmark = benchmark_de_teste(
        etapas=(medir("etapa", lambda: "resultado"),),
        artefatos=(("indptr.npy", 42), ("indices.npy", 43)),
    )

    escrever_relatorio(benchmark, destino)

    for bloco in destino.read_text(encoding="utf-8").split("\n\n"):
        linhas = [linha for linha in bloco.splitlines() if linha.startswith("|")]
        if len(linhas) < 2:
            continue
        largura = linhas[0].count("|")
        assert all(linha.count("|") == largura for linha in linhas), bloco
