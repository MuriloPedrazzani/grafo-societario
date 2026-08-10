"""Extração: zip-slip, espaço medido antes, idempotência, streaming e ZIP64.

Os ZIPs maliciosos são construídos aqui, byte a byte, e não simulados. O `zipfile`
sanitiza nomes em `extract()`, mas a extração deste projeto usa `open()` para poder
ler em streaming — a proteção precisa ser própria, e precisa ser testada contra um
arquivo que realmente contém o nome perigoso.
"""

from __future__ import annotations

import hashlib
import json
import zipfile
from pathlib import Path

import pytest

from grafo_societario.config import Config
from grafo_societario.ingest import extract, manifesto
from grafo_societario.ingest.extract import (
    NOME_DO_MANIFESTO,
    EspacoInsuficienteError,
    ExtracaoInseguraError,
    extrair_competencia,
    medir_descomprimido,
    nome_de_saida,
    recusar_nome_perigoso,
    verificar_espaco,
)
from grafo_societario.ingest.manifesto import ModoDeVerificacao

CONTEUDO = ("cnpj_basico;razao_social;natureza_juridica\n" * 500).encode("utf-8")


@pytest.fixture
def config_local(tmp_path: Path) -> Config:
    return Config(competencia="2026-06", data_dir=tmp_path)


def pasta_bruto(config: Config) -> Path:
    caminho = config.data_dir / "bruto" / "2026-06"
    caminho.mkdir(parents=True, exist_ok=True)
    return caminho


def pasta_extraido(config: Config) -> Path:
    return config.data_dir / "extraido" / "2026-06"


def criar_zip(destino: Path, membros: dict[str, bytes]) -> Path:
    with zipfile.ZipFile(destino, "w", zipfile.ZIP_DEFLATED) as arquivo:
        for nome, conteudo in membros.items():
            arquivo.writestr(nome, conteudo)
    return destino


# --------------------------------------------------------------------------- zip-slip


@pytest.mark.parametrize(
    "nome_malicioso",
    [
        "../../fora.csv",
        "../vizinho.csv",
        "subpasta/../../fora.csv",
        "/etc/passwd",
        "C:\\Windows\\System32\\evil.csv",
        "..\\..\\fora.csv",
    ],
)
def test_nome_que_escapa_do_destino_e_recusado(nome_malicioso: str) -> None:
    with pytest.raises(ExtracaoInseguraError):
        recusar_nome_perigoso(Path("Empresas0.zip"), nome_malicioso)


@pytest.mark.parametrize(
    "nome_legitimo",
    [
        "K3241.K03200Y0.D60613.ESTABELE",
        "F.K03200$Z.D60613.QUALSCSV",
        "subpasta/arquivo.csv",
    ],
)
def test_nome_legitimo_passa(nome_legitimo: str) -> None:
    recusar_nome_perigoso(Path("Empresas0.zip"), nome_legitimo)


def test_zip_malicioso_nao_escreve_fora_do_destino(config_local: Config, tmp_path: Path) -> None:
    """O ZIP é construído de verdade, com o nome perigoso gravado no cabeçalho."""
    alvo_do_ataque = tmp_path / "fora.csv"
    criar_zip(pasta_bruto(config_local) / "Empresas0.zip", {"../../fora.csv": b"invadido"})

    with pytest.raises(ExtracaoInseguraError, match=r"fora\.csv"):
        extrair_competencia(config_local)

    assert not alvo_do_ataque.exists()
    assert not (config_local.data_dir / "fora.csv").exists()


def test_zip_malicioso_e_recusado_antes_de_qualquer_escrita(config_local: Config) -> None:
    """A recusa acontece na medição, antes de o primeiro byte ser extraído."""
    bruto = pasta_bruto(config_local)
    criar_zip(bruto / "Cnaes.zip", {"F.K03200$Z.D60613.CNAECSV": CONTEUDO})
    criar_zip(bruto / "Empresas0.zip", {"../../fora.csv": b"invadido"})

    with pytest.raises(ExtracaoInseguraError):
        extrair_competencia(config_local)

    assert list(pasta_extraido(config_local).glob("*.csv")) == []


# --------------------------------------------------------------------------- espaço


def test_medicao_le_o_tamanho_declarado_sem_descomprimir(config_local: Config) -> None:
    caminho = criar_zip(pasta_bruto(config_local) / "Socios0.zip", {"K3241.SOCIOCSV": CONTEUDO})

    medidas = medir_descomprimido([caminho])

    assert medidas[caminho] == len(CONTEUDO)
    assert caminho.stat().st_size < len(CONTEUDO)


def test_falta_de_espaco_falha_com_o_numero_exato(tmp_path: Path) -> None:
    livre = __import__("shutil").disk_usage(tmp_path).free

    with pytest.raises(EspacoInsuficienteError) as capturado:
        verificar_espaco(tmp_path, livre + 5_000_000_000)

    mensagem = str(capturado.value)
    assert "Faltam" in mensagem
    assert "bytes" in mensagem
    assert "MANTER_ZIP" in mensagem


def test_espaco_suficiente_nao_levanta(tmp_path: Path) -> None:
    verificar_espaco(tmp_path, 1024)


# --------------------------------------------------------------------------- extração


def test_extrai_com_nome_previsivel_e_conteudo_intacto(config_local: Config) -> None:
    criar_zip(
        pasta_bruto(config_local) / "Empresas0.zip", {"K3241.K03200Y0.D60613.EMPRECSV": CONTEUDO}
    )

    extraidos = extrair_competencia(config_local)

    assert [caminho.name for caminho in extraidos] == ["Empresas0.csv"]
    assert extraidos[0].read_bytes() == CONTEUDO


def test_manifesto_preserva_o_nome_original_do_membro(config_local: Config) -> None:
    criar_zip(
        pasta_bruto(config_local) / "Socios3.zip", {"K3241.K03200Y3.D60613.SOCIOCSV": CONTEUDO}
    )

    extrair_competencia(config_local)

    conteudo = json.loads((pasta_extraido(config_local) / NOME_DO_MANIFESTO).read_text("utf-8"))
    entrada = conteudo["arquivos"]["Socios3.csv"]
    assert entrada["membro_original"] == "K3241.K03200Y3.D60613.SOCIOCSV"
    assert entrada["origem"] == "Socios3.zip"
    assert entrada["tamanho"] == len(CONTEUDO)
    assert entrada["sha256"] == hashlib.sha256(CONTEUDO).hexdigest()


def test_segunda_extracao_nao_reextrai(config_local: Config) -> None:
    criar_zip(pasta_bruto(config_local) / "Cnaes.zip", {"F.K03200$Z.D60613.CNAECSV": CONTEUDO})
    extrair_competencia(config_local)

    saida = pasta_extraido(config_local) / "Cnaes.csv"
    modificado_em = saida.stat().st_mtime_ns

    extrair_competencia(config_local)

    assert saida.stat().st_mtime_ns == modificado_em


def test_verificacao_completa_reextrai_arquivo_corrompido(config_local: Config) -> None:
    criar_zip(pasta_bruto(config_local) / "Cnaes.zip", {"F.K03200$Z.D60613.CNAECSV": CONTEUDO})
    extrair_competencia(config_local)

    saida = pasta_extraido(config_local) / "Cnaes.csv"
    saida.write_bytes(b"X" * len(CONTEUDO))

    extrair_competencia(config_local, modo=ModoDeVerificacao.RAPIDA)
    assert saida.read_bytes() != CONTEUDO

    extrair_competencia(config_local, modo=ModoDeVerificacao.COMPLETA)
    assert saida.read_bytes() == CONTEUDO


def test_nao_deixa_parcial_para_tras(config_local: Config) -> None:
    criar_zip(pasta_bruto(config_local) / "Paises.zip", {"F.K03200$Z.D60613.PAISCSV": CONTEUDO})

    extrair_competencia(config_local)

    assert list(pasta_extraido(config_local).glob("*.parcial")) == []


def test_zip_ausente_falha_com_mensagem_util(config_local: Config) -> None:
    pasta_bruto(config_local)
    with pytest.raises(extract.ErroDeExtracao, match="aquisição"):
        extrair_competencia(config_local)


def test_manter_zip_desligado_descarta_o_arquivo(tmp_path: Path) -> None:
    config = Config(competencia="2026-06", data_dir=tmp_path, manter_zip=False)
    zip_de_origem = criar_zip(pasta_bruto(config) / "Motivos.zip", {"F.MOTICSV": CONTEUDO})

    extrair_competencia(config)

    assert not zip_de_origem.exists()
    assert (pasta_extraido(config) / "Motivos.csv").read_bytes() == CONTEUDO


def test_manter_zip_ligado_preserva_o_arquivo(config_local: Config) -> None:
    zip_de_origem = criar_zip(pasta_bruto(config_local) / "Motivos.zip", {"F.MOTICSV": CONTEUDO})

    extrair_competencia(config_local)

    assert zip_de_origem.exists()


def test_varios_membros_recebem_nome_numerado(config_local: Config) -> None:
    criar_zip(
        pasta_bruto(config_local) / "Naturezas.zip",
        {"F.PRIMEIRO": CONTEUDO, "F.SEGUNDO": CONTEUDO},
    )

    extraidos = extrair_competencia(config_local)

    assert sorted(caminho.name for caminho in extraidos) == ["Naturezas-1.csv", "Naturezas-2.csv"]


def test_nome_de_saida_isolado() -> None:
    assert nome_de_saida(Path("Estabelecimentos0.zip"), 0, 1) == "Estabelecimentos0.csv"
    assert nome_de_saida(Path("Estabelecimentos0.zip"), 1, 3) == "Estabelecimentos0-2.csv"


# --------------------------------------------------------------------------- ZIP64


def test_membro_gravado_pelo_caminho_zip64_e_extraido(config_local: Config) -> None:
    """`Estabelecimentos0.zip` é ZIP64 por passar de 4 GiB.

    Construir 4 GiB aqui seria absurdo; `force_zip64=True` exercita o mesmo caminho
    de código do `zipfile` num arquivo minúsculo. Caminho sem teste é caminho que
    quebra em produção.
    """
    caminho = pasta_bruto(config_local) / "Estabelecimentos0.zip"
    with (
        zipfile.ZipFile(caminho, "w", zipfile.ZIP_DEFLATED) as arquivo,
        arquivo.open("K3241.K03200Y0.D60613.ESTABELE", "w", force_zip64=True) as membro,
    ):
        membro.write(CONTEUDO)

    with zipfile.ZipFile(caminho) as arquivo:
        info = arquivo.infolist()[0]
    assert info.header_offset >= 0
    assert info.file_size == len(CONTEUDO)

    extraidos = extrair_competencia(config_local)

    assert extraidos[0].name == "Estabelecimentos0.csv"
    assert extraidos[0].read_bytes() == CONTEUDO


def test_streaming_nao_carrega_o_membro_inteiro(
    config_local: Config, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A leitura tem de ser em blocos: um membro real tem 6,31 GiB."""
    grande = b"a" * (3 * 1024 * 1024 + 7)
    criar_zip(pasta_bruto(config_local) / "Municipios.zip", {"F.MUNICCSV": grande})

    leituras: list[int | None] = []
    original = zipfile.ZipExtFile.read

    def espiar(self: zipfile.ZipExtFile, tamanho: int | None = -1) -> bytes:
        leituras.append(tamanho)
        return original(self, tamanho)

    monkeypatch.setattr(zipfile.ZipExtFile, "read", espiar)
    extrair_competencia(config_local)

    assert leituras, "o membro deveria ter sido lido pela API de streaming"
    assert all(tamanho == 1024 * 1024 for tamanho in leituras)
    assert len(leituras) >= 4
    assert (pasta_extraido(config_local) / "Municipios.csv").read_bytes() == grande


def test_sha256_da_extracao_confere_com_o_conteudo(config_local: Config) -> None:
    criar_zip(pasta_bruto(config_local) / "Qualificacoes.zip", {"F.QUALSCSV": CONTEUDO})

    extrair_competencia(config_local)

    registro = extract.carregar_manifesto(pasta_extraido(config_local), "2026-06")
    entrada = registro.entradas["Qualificacoes.csv"]
    saida = pasta_extraido(config_local) / "Qualificacoes.csv"
    assert entrada.sha256 == manifesto.calcular_sha256(saida)
