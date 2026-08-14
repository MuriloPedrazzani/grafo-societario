"""A partida da aplicação: ela morre com o artefato quebrado, ou não sobe mentindo.

O teste central deste arquivo não consulta nada — ele **quebra um artefato e exige
que a aplicação não suba**. Health check verde com grafo não carregado é pior que
processo morto: um você percebe, o outro você descobre pelo usuário, depois de o
balanceador já ter mandado tráfego.
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import numpy as np
import pytest
from fastapi.testclient import TestClient

from grafo_societario.api.main import ErroDePartida, carregar_acervo, criar_aplicacao
from grafo_societario.config import Config
from grafo_societario.graph.artefatos import ARTEFATOS_PUBLICAVEIS, somas_dos_artefatos
from grafo_societario.graph.build import gerar_arestas, gerar_nos, serializar_csr
from grafo_societario.graph.catalogo import ArtefatoAusenteError, ArtefatosIncompativeisError
from grafo_societario.graph.components import calcular_componentes
from grafo_societario.graph.csr import ArtefatoAusenteError as CsrAusenteError
from grafo_societario.graph.metadados import serializar_metadados
from grafo_societario.transform.identity import gerar_identidades
from grafo_societario.transform.silver import (
    aplicar_recorte_por_uf,
    tipar_empresas,
    tipar_socios,
)
from test_silver import (
    NATUREZAS_PADRAO,
    PAISES_PADRAO,
    QUALIFICACOES_PADRAO,
    _gravar_dominio,
    empresa,
    estabelecimento,
    gravar_empresas,
    gravar_estabelecimentos,
    gravar_socios,
    socio,
)


@pytest.fixture
def pronto(tmp_path: Path) -> Config:
    """Um grafo completo em disco: duas empresas ligadas por uma pessoa física."""
    config = Config(competencia="2026-06", data_dir=tmp_path, uf_alvo="SP")
    gravar_estabelecimentos(
        config, [estabelecimento(cnpj) for cnpj in ("11111111", "22222222", "33333333")]
    )
    aplicar_recorte_por_uf(config)
    gravar_empresas(
        config,
        [
            empresa("11111111", razao_social="ALFA COMERCIO LTDA"),
            empresa("22222222", razao_social="BRAVO SERVICOS SA"),
            empresa("33333333", razao_social="CHARLIE SOZINHA ME"),
        ],
    )
    _gravar_dominio(config, "Naturezas", NATUREZAS_PADRAO)
    _gravar_dominio(config, "Qualificacoes", QUALIFICACOES_PADRAO)
    _gravar_dominio(config, "Paises", PAISES_PADRAO)
    tipar_empresas(config)
    gravar_socios(
        config,
        [
            socio("11111111", nome="FULANO DE TAL", documento="***123458**"),
            socio("22222222", nome="FULANO DE TAL", documento="***123458**"),
        ],
    )
    tipar_socios(config)
    gerar_identidades(config)
    gerar_nos(config)
    gerar_arestas(config)
    serializar_csr(config)
    calcular_componentes(config)
    serializar_metadados(config)
    return config


def pasta(config: Config) -> Path:
    return config.data_dir / "grafo" / "2026-06"


# ------------------------------------------------ a aplicação sobe e diz o que carregou


def test_o_health_diz_qual_dado_esta_no_ar(pronto: Config) -> None:
    with TestClient(criar_aplicacao(pronto)) as cliente:
        resposta = cliente.get("/health")

    assert resposta.status_code == 200
    corpo = resposta.json()
    assert corpo["status"] == "ok"
    assert corpo["competencia"] == "2026-06"
    assert corpo["uf_alvo"] == "SP"
    assert corpo["expor_pf"] is False
    assert corpo["grafo"]["nos"] == 3
    assert corpo["grafo"]["empresas_no_recorte"] == 3


def test_o_health_traz_a_soma_de_cada_artefato(pronto: Config) -> None:
    """A competência diz de qual mês é o dado; a soma diz qual execução o produziu.

    É o que responde "qual build está no ar" sem adivinhação — e só significa algo
    porque o projeto garante que duas construções sobre o mesmo silver dão os
    mesmos bytes.
    """
    with TestClient(criar_aplicacao(pronto)) as cliente:
        somas = cliente.get("/health").json()["artefatos"]

    assert set(somas) == set(ARTEFATOS_PUBLICAVEIS)
    assert somas == somas_dos_artefatos(pasta(pronto))
    assert all(len(soma) == 64 for soma in somas.values())


def test_a_soma_muda_quando_o_artefato_muda(pronto: Config) -> None:
    """Controle positivo da soma: um medidor que devolve sempre o mesmo valor não
    identifica build nenhum."""
    antes = somas_dos_artefatos(pasta(pronto))
    alvo = pasta(pronto) / "regiao_fiscal.npy"
    with alvo.open("wb") as arquivo:
        np.save(arquivo, np.zeros(3, dtype=np.int8), allow_pickle=False)

    depois = somas_dos_artefatos(pasta(pronto))

    assert depois["regiao_fiscal.npy"] != antes["regiao_fiscal.npy"]
    assert depois["indptr.npy"] == antes["indptr.npy"], "o resto não pode mudar"


def test_o_health_declara_a_versao_do_pacote(pronto: Config) -> None:
    from grafo_societario import __version__

    with TestClient(criar_aplicacao(pronto)) as cliente:
        assert cliente.get("/health").json()["versao"] == __version__


def test_a_documentacao_interativa_responde(pronto: Config) -> None:
    """`/docs` é o link que vai para o currículo; se ele não abre, a fase não
    entregou o que prometeu."""
    with TestClient(criar_aplicacao(pronto)) as cliente:
        assert cliente.get("/docs").status_code == 200
        assert cliente.get("/openapi.json").status_code == 200


# ------------------------------------------- a aplicação morre na partida


@pytest.mark.parametrize(
    "ausente", ["indptr.npy", "nomes.bin", "componentes.npy", "existencia.npy"]
)
def test_artefato_ausente_derruba_a_partida(pronto: Config, ausente: str) -> None:
    """Não é erro na primeira consulta: é a aplicação não existir.

    Se o `/health` responde, o grafo está mapeado e conferido — e é isso que torna
    a resposta dele digna de confiança.
    """
    (pasta(pronto) / ausente).unlink()

    with (
        pytest.raises((ArtefatoAusenteError, CsrAusenteError, ErroDePartida)),
        TestClient(criar_aplicacao(pronto)),
    ):
        pass


def test_catalogo_de_outra_execucao_derruba_a_partida(pronto: Config) -> None:
    """A conferência que só existe aqui: cada artefato valida a si mesmo, e
    ninguém sozinho vê o catálogo descrevendo outro número de nós."""
    alvo = pasta(pronto) / "atributos.npy"
    with alvo.open("wb") as arquivo:
        np.save(arquivo, np.zeros(99, dtype=np.int8), allow_pickle=False)

    with pytest.raises((ErroDePartida, ArtefatosIncompativeisError)):
        carregar_acervo(pronto)


def test_arrays_do_csr_desalinhados_derrubam_a_partida(pronto: Config) -> None:
    alvo = pasta(pronto) / "indices.npy"
    with alvo.open("wb") as arquivo:
        np.save(arquivo, np.zeros(3, dtype=np.int32), allow_pickle=False)

    with pytest.raises(Exception, match=r"execuções diferentes|posições"):
        carregar_acervo(pronto)


def test_competencia_sem_artefato_derruba_a_partida(pronto: Config) -> None:
    outra = pronto.model_copy(update={"competencia": "2026-07"})

    with pytest.raises((ArtefatoAusenteError, CsrAusenteError, ErroDePartida)):
        carregar_acervo(outra)


def test_o_acervo_carrega_uma_vez_e_registra_o_custo(pronto: Config) -> None:
    acervo = carregar_acervo(pronto)

    assert acervo.segundos_de_partida > 0
    assert acervo.nos == 3
    assert acervo.arestas == 2
    assert acervo.catalogo.nos == acervo.grafo.nos


# ------------------------- a fronteira entre serving e construção


def test_a_aplicacao_nao_carrega_motor_nem_leitor_de_parquet() -> None:
    """A regra que atravessa a fase inteira, afirmada no módulo que responde.

    A imagem da Fase 8 tem teto de 300 MB. Se a aplicação arrastasse DuckDB,
    SciPy ou pyarrow, o catálogo do commit anterior não teria razão de existir —
    o Parquet já estava lá.
    """
    codigo = (
        "import sys; import grafo_societario.api.main; "
        "print('duckdb' in sys.modules, 'scipy' in sys.modules, 'pyarrow' in sys.modules)"
    )

    saida = subprocess.run(
        [sys.executable, "-c", codigo], capture_output=True, text=True, check=True
    )

    assert saida.stdout.strip() == "False False False", saida.stdout
