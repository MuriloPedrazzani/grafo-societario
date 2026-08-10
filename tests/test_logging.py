"""Log estruturado: forma do JSON, propagação do run_id e idempotência.

Cada teste escreve num `StringIO` próprio, e a fixture devolve a raiz do logging
ao estado original — sem isso um teste contaminaria a saída do seguinte.
"""

from __future__ import annotations

import io
import json
import logging
from collections.abc import Iterator
from typing import Any

import pytest

from grafo_societario.logging_setup import configurar_logging, obter_run_id


@pytest.fixture(autouse=True)
def raiz_restaurada() -> Iterator[None]:
    raiz = logging.getLogger()
    handlers, nivel = list(raiz.handlers), raiz.level
    yield
    for handler in list(raiz.handlers):
        raiz.removeHandler(handler)
    for handler in handlers:
        raiz.addHandler(handler)
    raiz.setLevel(nivel)


def linhas(saida: io.StringIO) -> list[dict[str, Any]]:
    return [json.loads(linha) for linha in saida.getvalue().splitlines() if linha]


def test_registro_sai_como_json_com_os_campos_exigidos() -> None:
    saida = io.StringIO()
    run_id = configurar_logging(stream=saida)

    logging.getLogger("grafo_societario.ingest").info("baixando arquivo")

    (registro,) = linhas(saida)
    assert registro["level"] == "INFO"
    assert registro["logger"] == "grafo_societario.ingest"
    assert registro["message"] == "baixando arquivo"
    assert registro["run_id"] == run_id
    assert registro["timestamp"].endswith("+00:00")


def test_run_id_e_o_mesmo_em_toda_a_execucao() -> None:
    saida = io.StringIO()
    run_id = configurar_logging(stream=saida)

    logging.getLogger("a").info("primeira")
    logging.getLogger("b").warning("segunda")

    assert [r["run_id"] for r in linhas(saida)] == [run_id, run_id]
    assert obter_run_id() == run_id


def test_execucoes_diferentes_recebem_run_ids_diferentes() -> None:
    primeiro = configurar_logging(stream=io.StringIO())
    segundo = configurar_logging(stream=io.StringIO())

    assert primeiro != segundo


def test_run_id_pode_ser_imposto_de_fora() -> None:
    saida = io.StringIO()
    configurar_logging(run_id="execucao-de-teste", stream=saida)

    logging.getLogger("a").info("mensagem")

    assert linhas(saida)[0]["run_id"] == "execucao-de-teste"


def test_campos_extras_entram_no_json() -> None:
    saida = io.StringIO()
    configurar_logging(stream=saida)

    logging.getLogger("silver").info("recorte aplicado", extra={"uf": "SP", "empresas": 1_234_567})

    registro = linhas(saida)[0]
    assert registro["uf"] == "SP"
    assert registro["empresas"] == 1_234_567


def test_excecao_vira_campo_e_nao_quebra_o_json() -> None:
    saida = io.StringIO()
    configurar_logging(stream=saida)

    try:
        raise ValueError("competência ausente")
    except ValueError:
        logging.getLogger("config").exception("falha ao carregar")

    registro = linhas(saida)[0]
    assert registro["level"] == "ERROR"
    assert "ValueError" in registro["exception"]
    assert "competência ausente" in registro["exception"]


def test_nivel_filtra_o_que_e_emitido() -> None:
    saida = io.StringIO()
    configurar_logging(nivel=logging.WARNING, stream=saida)

    logging.getLogger("a").info("nao deve aparecer")
    logging.getLogger("a").warning("deve aparecer")

    assert [r["message"] for r in linhas(saida)] == ["deve aparecer"]


def test_reconfigurar_nao_duplica_a_saida() -> None:
    configurar_logging(stream=io.StringIO())
    saida = io.StringIO()
    configurar_logging(stream=saida)

    logging.getLogger("a").info("uma vez só")

    assert len(linhas(saida)) == 1
    assert len(logging.getLogger().handlers) == 1
