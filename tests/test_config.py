"""Configuração: padrões, validação e clareza da mensagem de erro.

Todos os testes passam `env_file=None`. Sem isso, um `.env` presente na máquina de
quem roda a suíte entraria na leitura e o resultado deixaria de ser determinístico.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from grafo_societario.config import ConfigInvalidaError, carregar_config

VARIAVEIS = ("COMPETENCIA", "UF_ALVO", "DATA_DIR")


@pytest.fixture(autouse=True)
def ambiente_limpo(monkeypatch: pytest.MonkeyPatch) -> None:
    for variavel in VARIAVEIS:
        monkeypatch.delenv(variavel, raising=False)


def test_le_os_tres_parametros_do_ambiente(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("COMPETENCIA", "2026-06")
    monkeypatch.setenv("UF_ALVO", "RJ")
    monkeypatch.setenv("DATA_DIR", "dados")

    config = carregar_config(env_file=None)

    assert config.competencia == "2026-06"
    assert config.uf_alvo == "RJ"
    assert config.data_dir == Path("dados")


def test_uf_e_diretorio_tem_padrao(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("COMPETENCIA", "2026-06")

    config = carregar_config(env_file=None)

    assert config.uf_alvo == "SP"
    assert config.data_dir == Path("data")


def test_uf_e_normalizada(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("COMPETENCIA", "2026-06")
    monkeypatch.setenv("UF_ALVO", " sp ")

    assert carregar_config(env_file=None).uf_alvo == "SP"


def test_competencia_ausente_produz_erro_acionavel() -> None:
    with pytest.raises(ConfigInvalidaError) as capturado:
        carregar_config(env_file=None)

    mensagem = str(capturado.value)
    assert "COMPETENCIA" in mensagem
    assert "obrigatória" in mensagem
    assert ".env.example" in mensagem


def test_competencia_malformada_diz_o_formato_esperado(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("COMPETENCIA", "06/2026")

    with pytest.raises(ConfigInvalidaError) as capturado:
        carregar_config(env_file=None)

    mensagem = str(capturado.value)
    assert "COMPETENCIA" in mensagem
    assert "AAAA-MM" in mensagem


@pytest.mark.parametrize("invalida", ["XX", "São Paulo", ""])
def test_uf_desconhecida_e_recusada(monkeypatch: pytest.MonkeyPatch, invalida: str) -> None:
    monkeypatch.setenv("COMPETENCIA", "2026-06")
    monkeypatch.setenv("UF_ALVO", invalida)

    with pytest.raises(ConfigInvalidaError) as capturado:
        carregar_config(env_file=None)

    assert "UF_ALVO" in str(capturado.value)


def test_erro_reune_todas_as_variaveis_erradas(monkeypatch: pytest.MonkeyPatch) -> None:
    """Quem executa corrige tudo de uma vez, em vez de descobrir um erro por vez."""
    monkeypatch.setenv("UF_ALVO", "XX")

    mensagem = ""
    with pytest.raises(ConfigInvalidaError) as capturado:
        carregar_config(env_file=None)
    mensagem = str(capturado.value)

    assert "COMPETENCIA" in mensagem
    assert "UF_ALVO" in mensagem
