"""CLI: código de saída, orquestração, resolução de competência e adaptação ao destino.

A CLI orquestra e não trabalha, então o que se verifica aqui é: para onde ela
delega, com quais argumentos, e que número ela devolve ao shell. As regras de
negócio têm testes próprios nos módulos que as contêm.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
from typer.testing import CliRunner

from grafo_societario.cli import ERRO_DE_USO, FALHA, SUCESSO, Progresso, app
from grafo_societario.config import Config
from grafo_societario.ingest import downloader, extract
from grafo_societario.ingest.manifesto import ModoDeVerificacao

runner = CliRunner()

VARIAVEIS = ("COMPETENCIA", "UF_ALVO", "DATA_DIR", "RFB_URL_BASE", "RFB_TOKEN_COMPARTILHAMENTO")


@pytest.fixture(autouse=True)
def ambiente_limpo(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    for variavel in VARIAVEIS:
        monkeypatch.delenv(variavel, raising=False)
    monkeypatch.chdir(tmp_path)


@pytest.fixture
def chamadas(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> dict[str, Any]:
    """Substitui o trabalho de verdade, preservando a assinatura."""
    registro: dict[str, Any] = {}

    def baixar(
        config: Config,
        competencia: str | None = None,
        modo: ModoDeVerificacao = ModoDeVerificacao.RAPIDA,
        ao_progredir: Any = None,
    ) -> list[Path]:
        registro["baixar"] = {"competencia": config.competencia, "modo": modo}
        if ao_progredir is not None:
            ao_progredir("Cnaes.zip", 1, 1)
        return [tmp_path / "Cnaes.zip"]

    def extrair(
        config: Config,
        competencia: str | None = None,
        modo: ModoDeVerificacao = ModoDeVerificacao.RAPIDA,
        ao_progredir: Any = None,
    ) -> list[Path]:
        registro["extrair"] = {"competencia": config.competencia, "modo": modo}
        return [tmp_path / "Cnaes.csv"]

    monkeypatch.setattr(downloader, "baixar_competencia", baixar)
    monkeypatch.setattr(extract, "extrair_competencia", extrair)
    return registro


# --------------------------------------------------------------- código de saída


def test_sucesso_sai_com_zero(chamadas: dict[str, Any]) -> None:
    resultado = runner.invoke(app, ["ingest", "--competencia", "2026-06"])

    assert resultado.exit_code == SUCESSO
    assert chamadas["baixar"]["competencia"] == "2026-06"
    assert chamadas["extrair"]["competencia"] == "2026-06"


def test_falha_de_ingestao_sai_diferente_de_zero(monkeypatch: pytest.MonkeyPatch) -> None:
    def explodir(*_: object, **__: object) -> list[Path]:
        raise downloader.CompetenciaIncompletaError("faltam 3 arquivos")

    monkeypatch.setattr(downloader, "baixar_competencia", explodir)

    resultado = runner.invoke(app, ["ingest", "--competencia", "2026-08"])

    assert resultado.exit_code == FALHA
    assert "faltam 3 arquivos" in resultado.output


def test_erro_de_disco_sai_diferente_de_zero(monkeypatch: pytest.MonkeyPatch) -> None:
    def explodir(*_: object, **__: object) -> list[Path]:
        raise OSError("disco cheio")

    monkeypatch.setattr(downloader, "baixar_competencia", explodir)

    resultado = runner.invoke(app, ["ingest", "--competencia", "2026-06"])

    assert resultado.exit_code == FALHA


def test_competencia_ausente_sai_com_erro_de_uso() -> None:
    resultado = runner.invoke(app, ["ingest"])

    assert resultado.exit_code == ERRO_DE_USO
    assert "COMPETENCIA" in resultado.output


def test_competencia_malformada_sai_com_erro_de_uso() -> None:
    resultado = runner.invoke(app, ["ingest", "--competencia", "junho/2026"])

    assert resultado.exit_code == ERRO_DE_USO
    assert "AAAA-MM" in resultado.output


def test_competencia_e_ultima_juntas_sao_recusadas() -> None:
    resultado = runner.invoke(app, ["ingest", "--competencia", "2026-06", "--ultima"])

    assert resultado.exit_code == ERRO_DE_USO
    assert "não os dois" in resultado.output


# --------------------------------------------------------------- orquestração


def test_verificar_integridade_repassa_o_modo_completo(chamadas: dict[str, Any]) -> None:
    resultado = runner.invoke(
        app, ["ingest", "--competencia", "2026-06", "--verificar-integridade"]
    )

    assert resultado.exit_code == SUCESSO
    assert chamadas["baixar"]["modo"] is ModoDeVerificacao.COMPLETA
    assert chamadas["extrair"]["modo"] is ModoDeVerificacao.COMPLETA


def test_padrao_usa_verificacao_rapida(chamadas: dict[str, Any]) -> None:
    runner.invoke(app, ["ingest", "--competencia", "2026-06"])

    assert chamadas["baixar"]["modo"] is ModoDeVerificacao.RAPIDA


def test_sem_extrair_nao_chama_a_extracao(chamadas: dict[str, Any]) -> None:
    resultado = runner.invoke(app, ["ingest", "--competencia", "2026-06", "--sem-extrair"])

    assert resultado.exit_code == SUCESSO
    assert "baixar" in chamadas
    assert "extrair" not in chamadas


def test_competencia_vem_do_ambiente_quando_nao_e_passada(
    chamadas: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("COMPETENCIA", "2025-12")

    resultado = runner.invoke(app, ["ingest"])

    assert resultado.exit_code == SUCESSO
    assert chamadas["baixar"]["competencia"] == "2025-12"


# --------------------------------------------------------------- --ultima


def test_ultima_resolve_para_competencia_concreta_e_a_anuncia(
    chamadas: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    """Nada de escolher em silêncio: a competência resolvida aparece na saída."""
    monkeypatch.setattr(downloader, "criar_cliente", lambda _config: _ClienteFalso())
    monkeypatch.setattr(downloader, "resolver_competencia_mais_recente", lambda _cliente: "2026-07")

    resultado = runner.invoke(app, ["ingest", "--ultima"])

    assert resultado.exit_code == SUCESSO
    assert "2026-07" in resultado.output
    assert chamadas["baixar"]["competencia"] == "2026-07"


def test_ultima_sem_competencia_completa_sai_diferente_de_zero(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def recusar(_cliente: object) -> str:
        raise downloader.CompetenciaIncompletaError("Nenhuma competência está completa.")

    monkeypatch.setattr(downloader, "criar_cliente", lambda _config: _ClienteFalso())
    monkeypatch.setattr(downloader, "resolver_competencia_mais_recente", recusar)

    resultado = runner.invoke(app, ["ingest", "--ultima"])

    assert resultado.exit_code == FALHA
    assert "Nenhuma competência" in resultado.output


class _ClienteFalso:
    def __enter__(self) -> _ClienteFalso:
        return self

    def __exit__(self, *_: object) -> None:
        return None


# --------------------------------------------------------------- saída adaptada


def test_progresso_calado_quando_nao_ha_terminal(capsys: pytest.CaptureFixture[str]) -> None:
    progresso = Progresso("baixando", ativo=False)
    progresso("Empresas0.zip", 1, 36)
    progresso.encerrar()

    assert capsys.readouterr().err == ""


def test_progresso_escreve_quando_ha_terminal(capsys: pytest.CaptureFixture[str]) -> None:
    progresso = Progresso("baixando", ativo=True)
    progresso("Empresas0.zip", 3, 36)
    progresso.encerrar()

    saida = capsys.readouterr().err
    assert "Empresas0.zip" in saida
    assert "[ 3/36]" in saida
    assert saida.startswith("\r")
    assert saida.endswith("\n")


def test_sem_terminal_a_saida_nao_tem_retorno_de_carro(chamadas: dict[str, Any]) -> None:
    """CliRunner não é TTY: o resultado precisa ser legível como log."""
    resultado = runner.invoke(app, ["ingest", "--competencia", "2026-06"])

    assert "\r" not in resultado.output


def test_versao_responde_e_sai_com_zero() -> None:
    resultado = runner.invoke(app, ["versao"])

    assert resultado.exit_code == SUCESSO
    assert resultado.output.strip()
