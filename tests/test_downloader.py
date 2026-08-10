"""Download: lista de permissão, competência incompleta, retentativa e retomada.

Os testes conversam com um servidor HTTP real (ver `conftest.py`), não com um mock
de biblioteca. O que está sendo verificado é a negociação com o servidor.
"""

from __future__ import annotations

import base64
from pathlib import Path

import httpx
import pytest

from conftest import EstadoDoServidor
from grafo_societario.config import Config
from grafo_societario.ingest import downloader
from grafo_societario.ingest.downloader import (
    ARQUIVOS_ESPERADOS,
    CompetenciaIncompletaError,
    FalhaTransitoriaError,
    baixar_arquivo,
    baixar_competencia,
    criar_cliente,
    listar_arquivos,
    listar_competencias,
    validar_competencia,
)

CONTEUDO = b"PK\x03\x04" + b"conteudo de teste " * 64


@pytest.fixture(autouse=True)
def sem_espera_entre_tentativas(monkeypatch: pytest.MonkeyPatch) -> None:
    """Exercita a retentativa sem gastar o tempo de relógio do backoff real."""
    monkeypatch.setattr(downloader, "ESPERA_MINIMA", 0.0)
    monkeypatch.setattr(downloader, "ESPERA_MAXIMA", 0.0)


def competencia_completa() -> dict[str, bytes]:
    return {nome: CONTEUDO for nome in ARQUIVOS_ESPERADOS}


def test_lista_competencias_ignora_arquivo_solto_na_raiz(
    servidor: tuple[str, EstadoDoServidor], config_de_teste: Config
) -> None:
    _, estado = servidor
    estado.competencias = {"2026-05": {}, "2026-06": {}}
    estado.arquivos_na_raiz = {"cnpj.tar.gz": 63_954_782_749}

    with criar_cliente(config_de_teste) as cliente:
        assert listar_competencias(cliente) == ["2026-05", "2026-06"]


def test_lista_de_permissao_descarta_o_que_nao_e_esperado(
    servidor: tuple[str, EstadoDoServidor], config_de_teste: Config
) -> None:
    _, estado = servidor
    arquivos = competencia_completa()
    arquivos["cnpj.tar.gz"] = b"nao deve ser baixado"
    arquivos["Simples.zip"] = b"fora do escopo do MVP"
    arquivos["ArquivoNovoQueAlguemAdicionou.zip"] = b"o que ninguem previu"
    estado.competencias = {"2026-06": arquivos}

    with criar_cliente(config_de_teste) as cliente:
        encontrados = listar_arquivos(cliente, "2026-06")

    assert set(encontrados) == ARQUIVOS_ESPERADOS
    assert len(encontrados) == 36


def test_competencia_completa_e_aceita(
    servidor: tuple[str, EstadoDoServidor], config_de_teste: Config
) -> None:
    _, estado = servidor
    estado.competencias = {"2026-06": competencia_completa()}

    with criar_cliente(config_de_teste) as cliente:
        validar_competencia("2026-06", listar_arquivos(cliente, "2026-06"))


def test_competencia_em_envio_falha_alto(
    servidor: tuple[str, EstadoDoServidor], config_de_teste: Config
) -> None:
    """Pasta publicada não significa envio concluído."""
    _, estado = servidor
    arquivos = competencia_completa()
    del arquivos["Socios9.zip"]
    del arquivos["Estabelecimentos7.zip"]
    estado.competencias = {"2026-08": arquivos}

    with (
        criar_cliente(config_de_teste) as cliente,
        pytest.raises(CompetenciaIncompletaError) as capturado,
    ):
        validar_competencia("2026-08", listar_arquivos(cliente, "2026-08"))

    mensagem = str(capturado.value)
    assert "2026-08" in mensagem
    assert "34 dos 36" in mensagem
    assert "Estabelecimentos7.zip" in mensagem
    assert "Socios9.zip" in mensagem


def test_envia_basic_auth_com_o_token_do_compartilhamento(
    servidor: tuple[str, EstadoDoServidor], config_de_teste: Config
) -> None:
    _, estado = servidor
    estado.competencias = {"2026-06": {}}

    with criar_cliente(config_de_teste) as cliente:
        listar_arquivos(cliente, "2026-06")

    cabecalhos = estado.cabecalhos_de("PROPFIND", "/2026-06/")
    esperado = base64.b64encode(b"token-de-teste:").decode()
    assert cabecalhos["authorization"] == f"Basic {esperado}"
    assert cabecalhos["depth"] == "1"


def test_baixa_a_competencia_inteira(
    servidor: tuple[str, EstadoDoServidor], config_de_teste: Config
) -> None:
    _, estado = servidor
    estado.competencias = {"2026-06": competencia_completa()}

    baixados = baixar_competencia(config_de_teste)

    assert len(baixados) == 36
    assert all(caminho.read_bytes() == CONTEUDO for caminho in baixados)
    destino = config_de_teste.data_dir / "bruto" / "2026-06"
    assert list(destino.glob("*.parcial")) == []


def test_retenta_apos_falha_intermitente(
    servidor: tuple[str, EstadoDoServidor], config_de_teste: Config, tmp_path: Path
) -> None:
    _, estado = servidor
    estado.competencias = {"2026-06": {"Socios0.zip": CONTEUDO}}
    estado.falhas_restantes = 2

    with criar_cliente(config_de_teste) as cliente:
        arquivo = listar_arquivos(cliente, "2026-06")["Socios0.zip"]
        destino = baixar_arquivo(cliente, arquivo, tmp_path)

    assert destino.read_bytes() == CONTEUDO
    tentativas = [pedido for pedido in estado.requisicoes if pedido[0] == "GET"]
    assert len(tentativas) == 3


def test_retoma_parcial_enviando_range_e_if_range(
    servidor: tuple[str, EstadoDoServidor], config_de_teste: Config, tmp_path: Path
) -> None:
    _, estado = servidor
    estado.competencias = {"2026-06": {"Empresas0.zip": CONTEUDO}}

    parcial = tmp_path / "Empresas0.zip.parcial"
    parcial.write_bytes(CONTEUDO[:100])

    with criar_cliente(config_de_teste) as cliente:
        arquivo = listar_arquivos(cliente, "2026-06")["Empresas0.zip"]
        destino = baixar_arquivo(cliente, arquivo, tmp_path)

    assert destino.read_bytes() == CONTEUDO
    cabecalhos = estado.cabecalhos_de("GET", "Empresas0.zip")
    assert cabecalhos["range"] == "bytes=100-"
    assert cabecalhos["if-range"] == '"etag-de-Empresas0.zip"'


def test_arquivo_alterado_na_origem_recomeca_do_zero(
    servidor: tuple[str, EstadoDoServidor], config_de_teste: Config, tmp_path: Path
) -> None:
    """Se o ETag mudou, o parcial em disco é de outro arquivo e precisa ser descartado."""
    _, estado = servidor
    estado.competencias = {"2026-06": {"Empresas0.zip": CONTEUDO}}

    parcial = tmp_path / "Empresas0.zip.parcial"
    parcial.write_bytes(b"restos de um arquivo antigo")

    with criar_cliente(config_de_teste) as cliente:
        arquivo = listar_arquivos(cliente, "2026-06")["Empresas0.zip"]
        alterado = downloader.ArquivoRemoto(
            nome=arquivo.nome,
            caminho=arquivo.caminho,
            tamanho=arquivo.tamanho,
            etag='"etag-de-uma-versao-antiga"',
        )
        destino = baixar_arquivo(cliente, alterado, tmp_path)

    assert destino.read_bytes() == CONTEUDO
    assert b"restos" not in destino.read_bytes()


def test_arquivo_truncado_e_recusado_e_o_parcial_some(
    servidor: tuple[str, EstadoDoServidor], config_de_teste: Config, tmp_path: Path
) -> None:
    """Servidor anuncia um tamanho e entrega outro: aceitar seria corromper em silêncio."""
    _, estado = servidor
    estado.competencias = {"2026-06": {"Cnaes.zip": CONTEUDO}}
    estado.tamanho_anunciado = {"Cnaes.zip": len(CONTEUDO) + 5_000}

    with criar_cliente(config_de_teste) as cliente:
        arquivo = listar_arquivos(cliente, "2026-06")["Cnaes.zip"]
        with pytest.raises(FalhaTransitoriaError, match="truncado"):
            baixar_arquivo(cliente, arquivo, tmp_path)

    assert not (tmp_path / "Cnaes.zip.parcial").exists()
    assert not (tmp_path / "Cnaes.zip").exists()


def test_erro_de_cliente_nao_e_retentado(
    servidor: tuple[str, EstadoDoServidor], config_de_teste: Config, tmp_path: Path
) -> None:
    """404 não melhora com insistência; insistir só desperdiça o servidor da Receita."""
    _, estado = servidor
    estado.competencias = {"2026-06": {"Paises.zip": CONTEUDO}}

    with criar_cliente(config_de_teste) as cliente:
        ausente = downloader.ArquivoRemoto(
            nome="Paises.zip",
            caminho="/2026-06/NaoExiste.zip",
            tamanho=len(CONTEUDO),
            etag='"etag-de-Paises.zip"',
        )
        with pytest.raises(httpx.HTTPStatusError):
            baixar_arquivo(cliente, ausente, tmp_path)

    assert len([pedido for pedido in estado.requisicoes if pedido[0] == "GET"]) == 1
