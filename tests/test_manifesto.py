"""Manifesto: ida e volta, gravação atômica e resistência a arquivo corrompido."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path

from grafo_societario.ingest.manifesto import (
    NOME_DO_ARQUIVO,
    EntradaDoManifesto,
    Manifesto,
    calcular_sha256,
    carregar,
    gravar,
)


def entrada_de_exemplo(nome: str = "Socios0.zip") -> EntradaDoManifesto:
    return EntradaDoManifesto.agora(
        nome=nome,
        tamanho=236_178_314,
        sha256="a" * 64,
        etag='"46c0e6ce324c07c87b8688f4024522c4"',
        last_modified="Sun, 14 Jun 2026 19:16:51 GMT",
    )


def test_manifesto_ausente_devolve_registro_vazio(tmp_path: Path) -> None:
    assert carregar(tmp_path, "2026-06").entradas == {}


def test_grava_e_le_de_volta_todos_os_campos(tmp_path: Path) -> None:
    registro = Manifesto(competencia="2026-06")
    original = entrada_de_exemplo()
    registro.registrar(original)
    gravar(registro, tmp_path)

    relido = carregar(tmp_path, "2026-06")

    assert relido.competencia == "2026-06"
    assert relido.entradas["Socios0.zip"] == original


def test_gravacao_nao_deixa_temporario_para_tras(tmp_path: Path) -> None:
    registro = Manifesto(competencia="2026-06")
    registro.registrar(entrada_de_exemplo())
    gravar(registro, tmp_path)

    assert [caminho.name for caminho in tmp_path.iterdir()] == [NOME_DO_ARQUIVO]


def test_regravar_substitui_sem_deixar_manifesto_invalido(tmp_path: Path) -> None:
    primeiro = Manifesto(competencia="2026-06")
    primeiro.registrar(entrada_de_exemplo("Empresas0.zip"))
    gravar(primeiro, tmp_path)

    segundo = Manifesto(competencia="2026-06")
    segundo.registrar(entrada_de_exemplo("Empresas0.zip"))
    segundo.registrar(entrada_de_exemplo("Socios0.zip"))
    gravar(segundo, tmp_path)

    relido = carregar(tmp_path, "2026-06")
    assert set(relido.entradas) == {"Empresas0.zip", "Socios0.zip"}


def test_manifesto_corrompido_e_descartado_em_vez_de_explodir(tmp_path: Path) -> None:
    """Cache ilegível deve custar um download, não travar o pipeline."""
    (tmp_path / NOME_DO_ARQUIVO).write_text('{"arquivos": {"Socios0.zip": ', encoding="utf-8")

    assert carregar(tmp_path, "2026-06").entradas == {}


def test_manifesto_com_campo_faltando_e_descartado(tmp_path: Path) -> None:
    conteudo = {"competencia": "2026-06", "arquivos": {"Socios0.zip": {"tamanho": 10}}}
    (tmp_path / NOME_DO_ARQUIVO).write_text(json.dumps(conteudo), encoding="utf-8")

    assert carregar(tmp_path, "2026-06").entradas == {}


def test_calcular_sha256_confere_com_a_biblioteca_padrao(tmp_path: Path) -> None:
    conteudo = b"PK\x03\x04" + bytes(range(256)) * 500
    arquivo = tmp_path / "amostra.zip"
    arquivo.write_bytes(conteudo)

    assert calcular_sha256(arquivo) == hashlib.sha256(conteudo).hexdigest()
