"""Cache: rodar duas vezes não baixa duas vezes, e não custa reler tudo.

O critério do plano é "rodar duas vezes seguidas não baixa nada na segunda".
Aqui isso é medido contando requisições `GET` no servidor, e a ausência de
releitura é medida instrumentando a única função que lê o disco inteiro.
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path

import pytest

from conftest import EstadoDoServidor
from grafo_societario.config import Config
from grafo_societario.ingest import downloader, manifesto
from grafo_societario.ingest.downloader import ModoDeVerificacao, baixar_competencia
from grafo_societario.ingest.manifesto import NOME_DO_ARQUIVO

CONTEUDO = b"PK\x03\x04" + b"bytes de teste " * 128


@pytest.fixture(autouse=True)
def sem_espera_entre_tentativas(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(downloader, "ESPERA_MINIMA", 0.0)
    monkeypatch.setattr(downloader, "ESPERA_MAXIMA", 0.0)


@pytest.fixture
def competencia_pequena(servidor: tuple[str, EstadoDoServidor]) -> EstadoDoServidor:
    """Uma competência reduzida: o cache não depende de haver 36 arquivos."""
    _, estado = servidor
    estado.competencias = {"2026-06": {nome: CONTEUDO for nome in ("Cnaes.zip", "Paises.zip")}}
    return estado


@pytest.fixture(autouse=True)
def lista_de_permissao_reduzida(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(downloader, "ARQUIVOS_ESPERADOS", frozenset({"Cnaes.zip", "Paises.zip"}))


def gets(estado: EstadoDoServidor) -> int:
    return len([pedido for pedido in estado.requisicoes if pedido[0] == "GET"])


def test_segunda_execucao_nao_baixa_nada(
    competencia_pequena: EstadoDoServidor, config_de_teste: Config
) -> None:
    baixar_competencia(config_de_teste)
    apos_a_primeira = gets(competencia_pequena)

    baixar_competencia(config_de_teste)

    assert apos_a_primeira == 2
    assert gets(competencia_pequena) == 2


def test_verificacao_rapida_nao_le_o_conteudo_do_disco(
    competencia_pequena: EstadoDoServidor,
    config_de_teste: Config,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Não baixar não pode custar minutos de hash."""
    baixar_competencia(config_de_teste)

    def recusar(caminho: Path) -> str:
        raise AssertionError(f"a verificação rápida não deveria ler {caminho}")

    monkeypatch.setattr(manifesto, "calcular_sha256", recusar)
    baixar_competencia(config_de_teste)

    assert gets(competencia_pequena) == 2


def test_verificacao_completa_le_o_disco_e_aceita_arquivo_intacto(
    competencia_pequena: EstadoDoServidor, config_de_teste: Config
) -> None:
    baixar_competencia(config_de_teste)
    lidos: list[Path] = []
    original = manifesto.calcular_sha256

    def espiar(caminho: Path) -> str:
        lidos.append(caminho)
        return original(caminho)

    with pytest.MonkeyPatch.context() as patch:
        patch.setattr(manifesto, "calcular_sha256", espiar)
        baixar_competencia(config_de_teste, modo=ModoDeVerificacao.COMPLETA)

    assert len(lidos) == 2
    assert gets(competencia_pequena) == 2


def test_verificacao_completa_detecta_corrupcao_que_a_rapida_deixa_passar(
    competencia_pequena: EstadoDoServidor, config_de_teste: Config
) -> None:
    """Bytes trocados sem mudança de tamanho: exatamente o que o ETag não vê."""
    baixar_competencia(config_de_teste)
    corrompido = config_de_teste.data_dir / "bruto" / "2026-06" / "Cnaes.zip"
    corrompido.write_bytes(b"X" * len(CONTEUDO))

    baixar_competencia(config_de_teste)
    assert gets(competencia_pequena) == 2
    assert corrompido.read_bytes() != CONTEUDO

    baixar_competencia(config_de_teste, modo=ModoDeVerificacao.COMPLETA)
    assert gets(competencia_pequena) == 3
    assert corrompido.read_bytes() == CONTEUDO


def test_etag_novo_na_origem_forca_novo_download(
    competencia_pequena: EstadoDoServidor, config_de_teste: Config
) -> None:
    baixar_competencia(config_de_teste)
    competencia_pequena.etags["2026-06/Paises.zip"] = '"etag-da-competencia-republicada"'

    baixar_competencia(config_de_teste)

    assert gets(competencia_pequena) == 3


def test_arquivo_apagado_do_disco_e_baixado_de_novo(
    competencia_pequena: EstadoDoServidor, config_de_teste: Config
) -> None:
    baixar_competencia(config_de_teste)
    (config_de_teste.data_dir / "bruto" / "2026-06" / "Cnaes.zip").unlink()

    baixar_competencia(config_de_teste)

    assert gets(competencia_pequena) == 3


def test_sha256_do_manifesto_e_o_do_conteudo_baixado(
    competencia_pequena: EstadoDoServidor, config_de_teste: Config
) -> None:
    baixar_competencia(config_de_teste)

    destino = config_de_teste.data_dir / "bruto" / "2026-06"
    registro = manifesto.carregar(destino, "2026-06")

    assert registro.entradas["Cnaes.zip"].sha256 == hashlib.sha256(CONTEUDO).hexdigest()


def test_manifesto_guarda_os_campos_exigidos(
    competencia_pequena: EstadoDoServidor, config_de_teste: Config
) -> None:
    baixar_competencia(config_de_teste)

    destino = config_de_teste.data_dir / "bruto" / "2026-06"
    conteudo = json.loads((destino / NOME_DO_ARQUIVO).read_text(encoding="utf-8"))

    assert conteudo["competencia"] == "2026-06"
    entrada = conteudo["arquivos"]["Cnaes.zip"]
    assert set(entrada) == {"tamanho", "sha256", "etag", "last_modified", "baixado_em"}
    assert entrada["tamanho"] == len(CONTEUDO)
    assert entrada["last_modified"] == "Sun, 14 Jun 2026 19:07:57 GMT"
    assert entrada["etag"] == '"etag-de-Cnaes.zip"'


def test_arquivo_truncado_nao_entra_no_manifesto(
    competencia_pequena: EstadoDoServidor, config_de_teste: Config
) -> None:
    """Só download concluído e conferido é registrado."""
    competencia_pequena.tamanho_anunciado = {"Paises.zip": len(CONTEUDO) + 4_096}

    with pytest.raises(downloader.FalhaTransitoriaError, match="truncado"):
        baixar_competencia(config_de_teste)

    destino = config_de_teste.data_dir / "bruto" / "2026-06"
    registro = manifesto.carregar(destino, "2026-06")
    assert "Paises.zip" not in registro.entradas
    assert "Cnaes.zip" in registro.entradas


def test_retomada_produz_o_mesmo_hash_do_download_inteiro(
    competencia_pequena: EstadoDoServidor, config_de_teste: Config
) -> None:
    """O digestor é semeado com o parcial; retomar não pode alterar o hash."""
    destino = config_de_teste.data_dir / "bruto" / "2026-06"
    destino.mkdir(parents=True, exist_ok=True)
    (destino / "Cnaes.zip.parcial").write_bytes(CONTEUDO[:80])

    baixar_competencia(config_de_teste)

    registro = manifesto.carregar(destino, "2026-06")
    assert registro.entradas["Cnaes.zip"].sha256 == hashlib.sha256(CONTEUDO).hexdigest()
    assert (destino / "Cnaes.zip").read_bytes() == CONTEUDO
