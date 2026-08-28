"""A residente do processo, e as três condições que ela tem de respeitar.

O campo existe para ser o instrumento da soneira do commit 44 — a que responde se
a residente **estabiliza ou sobe** conforme as consultas cobrem mais do grafo.
Instrumento que devolve zero quando não sabe é pior que instrumento nenhum, então
as regras dele são testadas antes de ele ser usado para decidir qualquer coisa.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient

from grafo_societario.api import memoria
from grafo_societario.api.main import criar_aplicacao
from grafo_societario.config import Config
from test_caminho import grafo_de_exemplo  # noqa: F401


@pytest.fixture
def cliente(grafo_de_exemplo: Config) -> Any:  # noqa: F811
    with TestClient(criar_aplicacao(grafo_de_exemplo)) as aberto:
        yield aberto


def test_le_o_vmrss_em_bytes(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """O `/proc` publica em kB; o campo publica em bytes, para não haver unidade
    implícita atravessando a fronteira HTTP."""
    status = tmp_path / "status"
    status.write_text("Name:\tpython\nVmRSS:\t  291944 kB\nThreads:\t8\n", encoding="utf-8")
    monkeypatch.setattr(memoria, "STATUS", status)

    assert memoria.residente_em_bytes() == 291944 * 1024


@pytest.mark.parametrize(
    ("conteudo", "porque"),
    [
        ("Name:\tpython\nThreads:\t8\n", "arquivo sem a linha VmRSS"),
        ("VmRSS:\n", "linha VmRSS truncada"),
        ("VmRSS:\tnao-e-numero kB\n", "valor que não é número"),
    ],
)
def test_devolve_nulo_e_nunca_zero_quando_nao_sabe(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, conteudo: str, porque: str
) -> None:
    """Zero é um número, e número errado se propaga como se fosse medição.

    Foi o `0,00 GiB` de uma leitura de memória não conferida que ensinou isto ao
    projeto. `None` obriga quem consome a tratar a ausência; zero deixa passar.
    """
    status = tmp_path / "status"
    status.write_text(conteudo, encoding="utf-8")
    monkeypatch.setattr(memoria, "STATUS", status)

    assert memoria.residente_em_bytes() is None, porque


def test_devolve_nulo_onde_nao_ha_proc(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Windows no desenvolvimento, Linux no deploy: a ausência é metade dos
    casos, não uma borda."""
    monkeypatch.setattr(memoria, "STATUS", tmp_path / "nao-existe")

    assert memoria.residente_em_bytes() is None


def test_o_health_responde_ok_mesmo_sem_a_medida(
    cliente: Any, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """**Relatório, não portão.** O campo não pode derrubar a saúde da instância:
    o que ele mede é uso, e uso alto não é falha."""
    monkeypatch.setattr(memoria, "STATUS", tmp_path / "nao-existe")

    resposta = cliente.get("/health")

    assert resposta.status_code == 200
    corpo = resposta.json()
    assert corpo["status"] == "ok"
    assert corpo["residente_bytes"] is None


def test_o_health_publica_a_medida_quando_ela_existe(
    cliente: Any, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Controle positivo: sem ele, o teste acima passaria com um campo que nunca
    devolve número nenhum."""
    status = tmp_path / "status"
    status.write_text("VmRSS:\t  123456 kB\n", encoding="utf-8")
    monkeypatch.setattr(memoria, "STATUS", status)

    corpo = cliente.get("/health").json()

    assert corpo["residente_bytes"] == 123456 * 1024
    assert corpo["status"] == "ok"


def test_o_campo_nao_traz_psutil_para_a_imagem() -> None:
    """`psutil` mediria isto numa linha e está fora do conjunto base por decisão.

    Trazê-lo de volta para um campo de relatório desfaria a separação de grupos
    que tirou 110 MB da imagem de deploy.
    """
    fonte = Path(memoria.__file__).read_text(encoding="utf-8") if memoria.__file__ else ""

    assert "import psutil" not in fonte
