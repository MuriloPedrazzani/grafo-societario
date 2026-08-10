"""Bronze: contagem assertiva, colunas como texto e limite de memória declarado.

A entrada destes testes **não** é uma segunda cópia da fixture em UTF-8. Ela é
derivada rodando o extrator real sobre a fixture em latin-1, que é a única cópia
versionada. Duas cópias divergiriam num commit futuro sem nada as comparar;
derivando, a divergência é impossível por construção — e a composição das duas
etapas passa a ser verificada sem custar um teste a mais.
"""

from __future__ import annotations

import csv
import zipfile
from collections.abc import Callable
from pathlib import Path
from typing import Any

import duckdb
import pytest

from grafo_societario.config import Config
from grafo_societario.ingest.extract import extrair_competencia
from grafo_societario.transform import bronze
from grafo_societario.transform.bronze import (
    COLUNAS_EMPRESAS,
    COLUNAS_ESTABELECIMENTOS,
    COLUNAS_SOCIOS,
    ContagemDivergenteError,
    EspacoInsuficienteError,
    abrir_conexao,
    contar_registros,
    converter_empresas,
    converter_estabelecimentos,
    converter_principais,
    converter_socios,
    verificar_espaco,
)

FIXTURES = Path(__file__).parent / "fixtures"


MEMBROS = {
    "Estabelecimentos0": "K3241.K03200Y0.D60613.ESTABELE",
    "Empresas0": "K3241.K03200Y0.D60613.EMPRECSV",
    "Socios0": "K3241.K03200Y0.D60613.SOCIOCSV",
}


@pytest.fixture
def competencia_extraida(tmp_path: Path) -> Config:
    """Empacota as fixtures em ZIP e roda o extrator real, que transcodifica."""
    config = Config(competencia="2026-06", data_dir=tmp_path)
    bruto = tmp_path / "bruto" / "2026-06"
    bruto.mkdir(parents=True)
    for nome, membro in MEMBROS.items():
        with zipfile.ZipFile(bruto / f"{nome}.zip", "w", zipfile.ZIP_DEFLATED) as arquivo:
            arquivo.writestr(membro, (FIXTURES / f"{nome}.csv").read_bytes())
    extrair_competencia(config)
    return config


def registros_da_fixture(nome: str = "Estabelecimentos0") -> list[list[str]]:
    with (FIXTURES / f"{nome}.csv").open(encoding="latin-1", newline="") as arquivo:
        return list(csv.reader(arquivo, delimiter=";", quotechar='"'))


def parquet(config: Config) -> Path:
    return config.data_dir / "bronze" / "2026-06" / "estabelecimentos0.parquet"


def unico(resultado: tuple[Any, ...] | None) -> Any:
    """Primeiro valor da única linha esperada. `fetchone` pode devolver None."""
    assert resultado is not None, "a consulta não devolveu linha"
    return resultado[0]


# ------------------------------------------------------- contagem como assertiva


def test_contagem_de_registros_sobrevive_a_conversao(competencia_extraida: Config) -> None:
    esperado = len(registros_da_fixture())

    gerados = converter_estabelecimentos(competencia_extraida)

    with duckdb.connect() as conexao:
        registros = unico(
            conexao.execute(
                f"SELECT count(*) FROM read_parquet('{gerados[0].as_posix()}')"
            ).fetchone()
        )
    assert registros == esperado


def test_quebra_de_linha_interna_nao_vira_registro_a_mais(competencia_extraida: Config) -> None:
    """O critério que denuncia parser que conta linha em vez de registro."""
    extraido = competencia_extraida.data_dir / "extraido" / "2026-06" / "Estabelecimentos0.csv"
    fisicas = extraido.read_bytes().count(b"\n")
    esperado = len(registros_da_fixture())
    assert fisicas > esperado, "a fixture precisa ter quebra de linha dentro de campo"

    gerados = converter_estabelecimentos(competencia_extraida)

    with duckdb.connect() as conexao:
        registros = unico(
            conexao.execute(
                f"SELECT count(*) FROM read_parquet('{gerados[0].as_posix()}')"
            ).fetchone()
        )
    assert registros == esperado
    assert registros != fisicas


def test_contagem_divergente_interrompe_a_conversao(
    competencia_extraida: Config, monkeypatch: pytest.MonkeyPatch
) -> None:
    chamadas = iter([10**9])

    def contagem_mentirosa(*_: object, **__: object) -> int:
        return next(chamadas, 0)

    monkeypatch.setattr(bronze, "contar_registros", contagem_mentirosa)

    with pytest.raises(ContagemDivergenteError, match="não pode perder nem inventar"):
        converter_estabelecimentos(competencia_extraida)

    assert not parquet(competencia_extraida).exists()
    assert not parquet(competencia_extraida).with_suffix(".parquet.parcial").exists()


# ------------------------------------------------------------ colunas como texto


def test_todas_as_colunas_sao_varchar_e_nomeadas(competencia_extraida: Config) -> None:
    gerados = converter_estabelecimentos(competencia_extraida)

    with duckdb.connect() as conexao:
        descricao = conexao.execute(
            f"DESCRIBE SELECT * FROM read_parquet('{gerados[0].as_posix()}')"
        ).fetchall()

    assert tuple(coluna[0] for coluna in descricao) == COLUNAS_ESTABELECIMENTOS
    assert {coluna[1] for coluna in descricao} == {"VARCHAR"}


def test_zero_a_esquerda_nao_e_perdido(competencia_extraida: Config) -> None:
    """Se algum tipo fosse inferido, '01' e '1' colidiriam."""
    gerados = converter_estabelecimentos(competencia_extraida)

    with duckdb.connect() as conexao:
        situacoes = {
            linha[0]
            for linha in conexao.execute(
                f"SELECT DISTINCT situacao_cadastral FROM read_parquet('{gerados[0].as_posix()}')"
            ).fetchall()
        }
    assert any(codigo.startswith("0") and len(codigo) > 1 for codigo in situacoes)


def test_separador_dentro_de_campo_nao_desloca_colunas(competencia_extraida: Config) -> None:
    esperados = {registro[16] for registro in registros_da_fixture() if ";" in registro[16]}
    assert esperados, "a fixture precisa ter ';' dentro de campo citado"

    gerados = converter_estabelecimentos(competencia_extraida)

    with duckdb.connect() as conexao:
        obtidos = {
            linha[0]
            for linha in conexao.execute(
                f"SELECT complemento FROM read_parquet('{gerados[0].as_posix()}') "
                "WHERE complemento LIKE '%;%'"
            ).fetchall()
        }
    assert obtidos == esperados


def test_conteudo_acentuado_chega_intacto(competencia_extraida: Config) -> None:
    """Compara contra o valor exato da fixture, e não contra uma coluna adivinhada."""
    acentuados = {
        (COLUNAS_ESTABELECIMENTOS[indice], campo)
        for registro in registros_da_fixture()
        for indice, campo in enumerate(registro)
        if any(0xC0 <= ord(caractere) <= 0xFF for caractere in campo)
    }
    assert acentuados, "a fixture precisa ter conteúdo acentuado"

    gerados = converter_estabelecimentos(competencia_extraida)

    with duckdb.connect() as conexao:
        for coluna, esperado in sorted(acentuados):
            quantos = unico(
                conexao.execute(
                    f"SELECT count(*) FROM read_parquet('{gerados[0].as_posix()}') "
                    f"WHERE {coluna} = ?",
                    [esperado],
                ).fetchone()
            )
            assert quantos > 0, f"{coluna}={esperado!r} não sobreviveu"


# --------------------------------------------------------------------- ADR-008


def test_ler_a_saida_transcodificada_como_latin1_precisa_falhar(
    competencia_extraida: Config,
) -> None:
    """Guarda do ADR-008.

    O byte 0x8F continua no arquivo, agora como segundo byte de C2 8F, e o
    decodificador latin-1 do DuckDB recusa a faixa de controle C1 inteira. Se este
    teste passar a não levantar, alguém trocou a codificação de volta e três dos
    trinta e seis arquivos reais voltarão a ser recusados por inteiro.
    """
    extraido = competencia_extraida.data_dir / "extraido" / "2026-06" / "Estabelecimentos0.csv"
    assert b"\xc2\x8f" in extraido.read_bytes()

    with duckdb.connect() as conexao, pytest.raises(duckdb.InvalidInputException):
        conexao.execute(
            f"SELECT count(*) FROM read_csv('{extraido.as_posix()}', delim=';', quote='\"', "
            "header=false, all_varchar=true, encoding='latin-1')"
        ).fetchone()


def test_a_mesma_saida_e_lida_sem_erro_em_utf8(competencia_extraida: Config) -> None:
    """O outro lado do guarda: em UTF-8 o mesmo arquivo passa."""
    extraido = competencia_extraida.data_dir / "extraido" / "2026-06" / "Estabelecimentos0.csv"

    with duckdb.connect() as conexao:
        registros = unico(
            conexao.execute(
                f"SELECT count(*) FROM read_csv('{extraido.as_posix()}', delim=';', quote='\"', "
                "header=false, all_varchar=true, encoding='utf-8')"
            ).fetchone()
        )
    assert registros == len(registros_da_fixture())


# ----------------------------------------------------------- memória e espaço


def test_limite_de_memoria_e_o_declarado(tmp_path: Path) -> None:
    config = Config(competencia="2026-06", data_dir=tmp_path, limite_de_memoria="4GB")

    with abrir_conexao(config, tmp_path / "tmp") as conexao:
        limite = unico(conexao.execute("SELECT current_setting('memory_limit')").fetchone())
        temporario = unico(conexao.execute("SELECT current_setting('temp_directory')").fetchone())

    # O DuckDB reporta em GiB e interpreta "GB" como decimal: 4GB são 3,7 GiB.
    # A diferença joga a favor da promessa de 8 GiB, então fica como está.
    valor, unidade = limite.split()
    assert unidade == "GiB"
    assert 3.5 <= float(valor) <= 4.0
    assert Path(temporario).name == "tmp"


def test_padrao_de_memoria_respeita_a_promessa_de_oito_gib() -> None:
    """8 GiB é a restrição do projeto; o motor não pode reivindicar quase tudo."""
    assert Config(competencia="2026-06").limite_de_memoria == "4GB"


def test_espaco_insuficiente_recusa_antes_de_converter(tmp_path: Path) -> None:
    livre = __import__("shutil").disk_usage(tmp_path).free

    with pytest.raises(EspacoInsuficienteError, match="Faltam"):
        verificar_espaco(tmp_path, livre + 10_000_000_000)


def test_sem_csv_de_entrada_a_mensagem_e_util(tmp_path: Path) -> None:
    config = Config(competencia="2026-06", data_dir=tmp_path)
    (tmp_path / "extraido" / "2026-06").mkdir(parents=True)

    with pytest.raises(bronze.ErroDeBronze, match="ingestão"):
        converter_estabelecimentos(config)


# ------------------------------------------------------- Empresas e Socios


@pytest.mark.parametrize(
    ("converter", "fixture", "parquet_nome", "colunas"),
    [
        (converter_empresas, "Empresas0", "empresas0.parquet", COLUNAS_EMPRESAS),
        (converter_socios, "Socios0", "socios0.parquet", COLUNAS_SOCIOS),
    ],
)
def test_tabela_converte_com_colunas_declaradas_e_contagem_intacta(
    competencia_extraida: Config,
    converter: Callable[[Config], list[Path]],
    fixture: str,
    parquet_nome: str,
    colunas: tuple[str, ...],
) -> None:
    esperado = len(registros_da_fixture(fixture))

    gerados = converter(competencia_extraida)

    assert [caminho.name for caminho in gerados] == [parquet_nome]
    with duckdb.connect() as conexao:
        descricao = conexao.execute(
            f"DESCRIBE SELECT * FROM read_parquet('{gerados[0].as_posix()}')"
        ).fetchall()
        registros = unico(
            conexao.execute(
                f"SELECT count(*) FROM read_parquet('{gerados[0].as_posix()}')"
            ).fetchone()
        )
    assert tuple(coluna[0] for coluna in descricao) == colunas
    assert {coluna[1] for coluna in descricao} == {"VARCHAR"}
    assert registros == esperado


def test_capital_social_continua_texto_com_virgula(competencia_extraida: Config) -> None:
    """Converter para decimal é trabalho do silver. Fidelidade vale para tipo também."""
    esperados = {registro[4] for registro in registros_da_fixture("Empresas0")}
    assert any("," in valor for valor in esperados), "a fixture precisa ter decimal com vírgula"

    gerados = converter_empresas(competencia_extraida)

    with duckdb.connect() as conexao:
        tipo = unico(
            conexao.execute(
                f"SELECT column_type FROM (DESCRIBE SELECT * FROM "
                f"read_parquet('{gerados[0].as_posix()}')) WHERE column_name = 'capital_social'"
            ).fetchone()
        )
        obtidos = {
            linha[0]
            for linha in conexao.execute(
                f"SELECT DISTINCT capital_social FROM read_parquet('{gerados[0].as_posix()}')"
            ).fetchall()
        }
    assert tipo == "VARCHAR"
    assert obtidos == esperados


def test_socios_preserva_a_mascara_do_cpf_e_o_zero_a_esquerda(
    competencia_extraida: Config,
) -> None:
    esperados = {registro[3] for registro in registros_da_fixture("Socios0") if registro[1] == "2"}
    assert esperados, "a fixture precisa ter sócio pessoa física"

    gerados = converter_socios(competencia_extraida)

    with duckdb.connect() as conexao:
        obtidos = {
            linha[0]
            for linha in conexao.execute(
                f"SELECT DISTINCT cnpj_cpf_socio FROM read_parquet('{gerados[0].as_posix()}') "
                "WHERE identificador_socio = '2'"
            ).fetchall()
        }
        faixas = {
            linha[0]
            for linha in conexao.execute(
                f"SELECT DISTINCT faixa_etaria FROM read_parquet('{gerados[0].as_posix()}')"
            ).fetchall()
        }
    assert obtidos == esperados
    assert all(valor.startswith("***") for valor in obtidos)
    assert all(isinstance(valor, str) for valor in faixas)


def test_converter_principais_gera_as_tres_tabelas(competencia_extraida: Config) -> None:
    resultado = converter_principais(competencia_extraida)

    assert set(resultado) == {"estabelecimentos", "empresas", "socios"}
    assert all(caminhos for caminhos in resultado.values())
    assert all(caminho.exists() for caminhos in resultado.values() for caminho in caminhos)


def test_contagem_do_csv_usa_as_colunas_declaradas(competencia_extraida: Config) -> None:
    extraido = competencia_extraida.data_dir / "extraido" / "2026-06" / "Estabelecimentos0.csv"
    config = competencia_extraida

    with abrir_conexao(config, config.data_dir / "tmp") as conexao:
        assert contar_registros(conexao, extraido, COLUNAS_ESTABELECIMENTOS) == len(
            registros_da_fixture()
        )
