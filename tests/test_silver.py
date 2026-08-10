"""Recorte por UF da matriz: o que entra, o que fica de fora e o que é contado.

Os casos que descrevem a fonte partem da fixture real, atravessando extração e
bronze — o mesmo caminho do dado de verdade. Os casos construídos para falhar
escrevem CSV sintético: fixture existe para descrever o que a fonte tem, não o
que ela poderia ter, e a distinção é a mesma já adotada em `test_bronze.py`.
"""

from __future__ import annotations

import csv
import logging
import zipfile
from pathlib import Path

import duckdb
import pytest

from grafo_societario.config import Config
from grafo_societario.ingest.extract import extrair_competencia
from grafo_societario.transform.bronze import COLUNAS_ESTABELECIMENTOS, converter_estabelecimentos
from grafo_societario.transform.silver import (
    BronzeAusenteError,
    CnpjBasicoDuplicadoError,
    RecorteVazioError,
    aplicar_recorte_por_uf,
    validar_cnpj_basico_unico,
)

FIXTURES = Path(__file__).parent / "fixtures"
MEMBRO = "K3241.K03200Y0.D60613.ESTABELE"

UF = 19
MATRIZ_FILIAL = 3
SITUACAO = 5


@pytest.fixture
def bronze_da_fixture(tmp_path: Path) -> Config:
    """Fixture real pelo caminho real: ZIP, extração transcodificada e bronze."""
    config = Config(competencia="2026-06", data_dir=tmp_path, uf_alvo="SP")
    bruto = tmp_path / "bruto" / "2026-06"
    bruto.mkdir(parents=True)
    with zipfile.ZipFile(bruto / "Estabelecimentos0.zip", "w", zipfile.ZIP_DEFLATED) as arquivo:
        arquivo.writestr(MEMBRO, (FIXTURES / "Estabelecimentos0.csv").read_bytes())
    extrair_competencia(config)
    converter_estabelecimentos(config)
    return config


def registros_da_fixture() -> list[list[str]]:
    """Instrumento independente: o esperado não sai da mesma consulta que o obtido."""
    with (FIXTURES / "Estabelecimentos0.csv").open(encoding="latin-1", newline="") as arquivo:
        return list(csv.reader(arquivo, delimiter=";", quotechar='"'))


def ler_recorte(caminho: Path) -> list[tuple[str, str, str]]:
    with duckdb.connect() as conexao:
        return [
            (str(linha[0]), str(linha[1]), str(linha[2]))
            for linha in conexao.execute(
                f"SELECT cnpj_basico, situacao_cadastral, uf FROM "
                f"read_parquet('{caminho.as_posix()}') ORDER BY cnpj_basico"
            ).fetchall()
        ]


def gravar_estabelecimentos(config: Config, registros: list[dict[str, str]]) -> None:
    """Escreve um Estabelecimentos sintético direto no extraído, já em UTF-8.

    Passa pelo bronze de verdade: o que o silver lê é Parquet gerado pelo mesmo
    conversor que roda em produção, e não um Parquet montado à mão que poderia
    divergir dele sem nada avisar.
    """
    destino = config.data_dir / "extraido" / config.competencia
    destino.mkdir(parents=True, exist_ok=True)
    with (destino / "Estabelecimentos0.csv").open("w", encoding="utf-8", newline="") as arquivo:
        escritor = csv.writer(
            arquivo, delimiter=";", quotechar='"', quoting=csv.QUOTE_ALL, lineterminator="\n"
        )
        for registro in registros:
            escritor.writerow([registro.get(coluna, "") for coluna in COLUNAS_ESTABELECIMENTOS])
    converter_estabelecimentos(config)


def estabelecimento(
    cnpj_basico: str,
    *,
    uf: str = "SP",
    matriz: bool = True,
    ordem: str = "0001",
    situacao: str = "02",
) -> dict[str, str]:
    return {
        "cnpj_basico": cnpj_basico,
        "cnpj_ordem": ordem,
        "identificador_matriz_filial": "1" if matriz else "2",
        "situacao_cadastral": situacao,
        "uf": uf,
    }


def gravar_parquet(conexao: duckdb.DuckDBPyConnection, caminho: Path, valores: str) -> Path:
    conexao.execute(
        f"COPY (SELECT * FROM (VALUES {valores}) AS t(cnpj_basico, situacao_cadastral, uf)) "
        f"TO '{caminho.as_posix()}' (FORMAT PARQUET)"
    )
    return caminho


# ------------------------------------------------------------- o que entra


def test_recorte_traz_exatamente_as_matrizes_da_uf_alvo(bronze_da_fixture: Config) -> None:
    esperado = {
        registro[0]
        for registro in registros_da_fixture()
        if registro[UF] == "SP" and registro[MATRIZ_FILIAL] == "1"
    }
    assert esperado, "a fixture precisa ter matriz em SP"

    recorte = aplicar_recorte_por_uf(bronze_da_fixture)

    assert recorte.empresas == len(esperado)
    assert {linha[0] for linha in ler_recorte(recorte.caminho)} == esperado


def test_filial_na_uf_alvo_nao_entra(bronze_da_fixture: Config) -> None:
    """O recorte é da empresa, não do endereço: filial em SP não faz empresa de SP."""
    filiais = {
        registro[0]
        for registro in registros_da_fixture()
        if registro[UF] == "SP" and registro[MATRIZ_FILIAL] == "2"
    }
    matrizes_sp = {
        registro[0]
        for registro in registros_da_fixture()
        if registro[UF] == "SP" and registro[MATRIZ_FILIAL] == "1"
    }
    so_filial = filiais - matrizes_sp
    assert so_filial, "a fixture precisa ter filial em SP sem matriz em SP"

    recorte = aplicar_recorte_por_uf(bronze_da_fixture)

    assert not so_filial & {linha[0] for linha in ler_recorte(recorte.caminho)}


def test_matriz_de_outra_uf_nao_entra(bronze_da_fixture: Config) -> None:
    de_fora = {
        registro[0]
        for registro in registros_da_fixture()
        if registro[UF] != "SP" and registro[MATRIZ_FILIAL] == "1"
    }
    assert de_fora, "a fixture precisa ter matriz fora de SP"

    recorte = aplicar_recorte_por_uf(bronze_da_fixture)

    assert not de_fora & {linha[0] for linha in ler_recorte(recorte.caminho)}


def test_empresa_baixada_permanece_no_recorte(bronze_da_fixture: Config) -> None:
    """Decisão explícita: vínculo de empresa que fechou continua sendo vínculo.

    Filtrar por situação cadastral é decisão da API. Se este teste passar a
    falhar, alguém moveu essa decisão para o silver — onde ela apaga o dado para
    todo mundo, em vez de escondê-lo de quem não pediu.
    """
    baixadas = {
        registro[0]
        for registro in registros_da_fixture()
        if registro[UF] == "SP" and registro[MATRIZ_FILIAL] == "1" and registro[SITUACAO] == "08"
    }
    assert baixadas, "a fixture precisa ter matriz baixada em SP"

    recorte = aplicar_recorte_por_uf(bronze_da_fixture)

    assert baixadas <= {linha[0] for linha in ler_recorte(recorte.caminho)}
    assert dict(recorte.situacoes)["08"] == len(baixadas)


def test_situacao_cadastral_preserva_o_zero_a_esquerda(bronze_da_fixture: Config) -> None:
    """O PDF oficial escreve `2` onde o arquivo traz `02`. Vale o arquivo."""
    recorte = aplicar_recorte_por_uf(bronze_da_fixture)

    codigos = {situacao for situacao, _ in recorte.situacoes}
    assert codigos <= {"01", "02", "03", "04", "08"}
    assert any(codigo.startswith("0") for codigo in codigos)
    assert {linha[1] for linha in ler_recorte(recorte.caminho)} == codigos


def test_recorte_carrega_a_uf_que_o_gerou(bronze_da_fixture: Config) -> None:
    """O caminho do arquivo não diz de qual UF ele é; o artefato precisa dizer."""
    recorte = aplicar_recorte_por_uf(bronze_da_fixture)

    assert recorte.uf == "SP"
    assert {linha[2] for linha in ler_recorte(recorte.caminho)} == {"SP"}


def test_recorte_e_o_mesmo_byte_a_byte_entre_execucoes(bronze_da_fixture: Config) -> None:
    """A Fase 4 precisa de índice determinístico; começa aqui e sai de graça."""
    primeiro = aplicar_recorte_por_uf(bronze_da_fixture).caminho.read_bytes()
    segundo = aplicar_recorte_por_uf(bronze_da_fixture).caminho.read_bytes()

    assert primeiro == segundo


def test_log_reporta_quantas_empresas_entraram(
    bronze_da_fixture: Config, caplog: pytest.LogCaptureFixture
) -> None:
    with caplog.at_level(logging.INFO, logger="grafo_societario.transform.silver"):
        recorte = aplicar_recorte_por_uf(bronze_da_fixture)

    (registro,) = [linha for linha in caplog.records if linha.message == "recorte por UF aplicado"]
    assert registro.empresas == recorte.empresas  # type: ignore[attr-defined]
    assert registro.uf_alvo == "SP"  # type: ignore[attr-defined]
    assert registro.matrizes_repetidas == 0  # type: ignore[attr-defined]


# ------------------------------------------------------ matriz repetida na fonte


def test_matriz_repetida_colapsa_e_e_contada(tmp_path: Path) -> None:
    """Um caso em 68,6 milhões na competência 2026-06 — defeito da fonte, não do pipeline.

    Colapsar é o único desfecho que permite processar o dado que existe. O que não
    pode é colapsar calado: o contador entra no log de toda execução.
    """
    config = Config(competencia="2026-06", data_dir=tmp_path, uf_alvo="SP")
    gravar_estabelecimentos(
        config,
        [
            estabelecimento("08314885", ordem="0047", situacao="02"),
            estabelecimento("08314885", ordem="0051", situacao="08"),
            estabelecimento("11111111", ordem="0001", situacao="02"),
        ],
    )

    recorte = aplicar_recorte_por_uf(config)

    assert recorte.empresas == 2
    assert recorte.matrizes_repetidas == 1
    linhas = ler_recorte(recorte.caminho)
    assert [linha[0] for linha in linhas] == ["08314885", "11111111"]
    # Desempate pelo menor cnpj_ordem: 0047 vem antes de 0051, e a escolha não
    # pode depender da ordem em que o motor leu as partições.
    assert dict((linha[0], linha[1]) for linha in linhas)["08314885"] == "02"


def test_matriz_unica_nao_e_contada_como_repetida(tmp_path: Path) -> None:
    """Controle positivo do contador: ele precisa saber devolver zero também."""
    config = Config(competencia="2026-06", data_dir=tmp_path, uf_alvo="SP")
    gravar_estabelecimentos(
        config,
        [
            estabelecimento("11111111"),
            estabelecimento("11111111", ordem="0002", matriz=False),
            estabelecimento("22222222"),
        ],
    )

    recorte = aplicar_recorte_por_uf(config)

    assert recorte.empresas == 2
    assert recorte.matrizes_repetidas == 0


# ------------------------------------------------------------- o que é recusado


def test_recorte_vazio_e_recusado(tmp_path: Path) -> None:
    """Sem isto, UF errada produz artefato vazio em toda etapa seguinte, sem erro."""
    config = Config(competencia="2026-06", data_dir=tmp_path, uf_alvo="AC")
    gravar_estabelecimentos(config, [estabelecimento("11111111", uf="SP")])

    with pytest.raises(RecorteVazioError, match="UF_ALVO"):
        aplicar_recorte_por_uf(config)


def test_uf_sem_matriz_mas_com_filial_tambem_e_vazia(tmp_path: Path) -> None:
    """A UF alvo ter estabelecimento não basta: o recorte é de matriz."""
    config = Config(competencia="2026-06", data_dir=tmp_path, uf_alvo="AC")
    gravar_estabelecimentos(config, [estabelecimento("11111111", uf="AC", matriz=False)])

    with pytest.raises(RecorteVazioError):
        aplicar_recorte_por_uf(config)


def test_cnpj_basico_duplicado_no_recorte_e_recusado(tmp_path: Path) -> None:
    """A guarda exercitada contra um caso construído para reprovar.

    O recorte agrega por `cnpj_basico`, então a duplicata não vem da fonte: viria
    de alguém trocar o `GROUP BY` por um `SELECT` direto. É contra essa mudança
    que a guarda existe, e uma validação que nunca reprovou não provou que sabe.
    """
    caminho = tmp_path / "recorte.parquet"
    with duckdb.connect() as conexao:
        gravar_parquet(conexao, caminho, "('11111111','02','SP'), ('11111111','08','SP')")

        with pytest.raises(CnpjBasicoDuplicadoError, match="multiplica linha"):
            validar_cnpj_basico_unico(conexao, caminho)


def test_recorte_integro_passa_pela_guarda(tmp_path: Path) -> None:
    """O outro lado do controle positivo: a guarda precisa aceitar o caso bom."""
    caminho = tmp_path / "recorte.parquet"
    with duckdb.connect() as conexao:
        gravar_parquet(conexao, caminho, "('11111111','02','SP'), ('22222222','08','SP')")

        validar_cnpj_basico_unico(conexao, caminho)


def test_recorte_reprovado_nao_deixa_arquivo_para_tras(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Artefato que reprovou não pode existir com o nome definitivo."""
    from grafo_societario.transform import silver

    config = Config(competencia="2026-06", data_dir=tmp_path, uf_alvo="SP")
    gravar_estabelecimentos(config, [estabelecimento("11111111")])

    def recusar(*_: object, **__: object) -> None:
        raise CnpjBasicoDuplicadoError("recusa forçada para exercitar a limpeza")

    monkeypatch.setattr(silver, "validar_cnpj_basico_unico", recusar)

    with pytest.raises(CnpjBasicoDuplicadoError):
        aplicar_recorte_por_uf(config)

    destino = config.data_dir / "silver" / "2026-06"
    assert not (destino / "recorte.parquet").exists()
    assert not list(destino.glob("*.parcial"))


def test_sem_bronze_a_mensagem_diz_o_que_fazer(tmp_path: Path) -> None:
    """Sem a checagem, quem esqueceu de rodar o bronze recebe um erro de I/O sobre
    um glob que não casou, e precisa deduzir a causa a partir dele."""
    config = Config(competencia="2026-06", data_dir=tmp_path, uf_alvo="SP")
    (tmp_path / "bronze" / "2026-06").mkdir(parents=True)

    with pytest.raises(BronzeAusenteError, match="Rode o bronze"):
        aplicar_recorte_por_uf(config)
