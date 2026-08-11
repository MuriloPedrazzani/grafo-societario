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
from decimal import Decimal
from pathlib import Path

import duckdb
import pytest

from grafo_societario.config import Config
from grafo_societario.ingest.extract import extrair_competencia
from grafo_societario.transform.bronze import (
    COLUNAS_DOMINIO,
    COLUNAS_EMPRESAS,
    COLUNAS_ESTABELECIMENTOS,
    VALIDACOES_DE_DOMINIO,
    Tabela,
    converter_empresas,
    converter_estabelecimentos,
    converter_tabela,
)
from grafo_societario.transform.silver import (
    COLUNAS_SILVER_EMPRESAS,
    MARCA_DE_SUPRESSAO,
    PADRAO_DE_CPF_PARTIDO,
    PADROES_DE_DOCUMENTO,
    BronzeAusenteError,
    CapitalSocialMalformadoError,
    CnpjBasicoDuplicadoError,
    EmpresaAusenteError,
    RecorteAusenteError,
    RecorteVazioError,
    aplicar_recorte_por_uf,
    definir_macros,
    tipar_empresas,
    validar_cnpj_basico_unico,
)
from test_fixtures import cpf_valido

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


def _gravar_csv(
    config: Config, nome: str, colunas: tuple[str, ...], registros: list[dict[str, str]]
) -> None:
    """Escreve um CSV sintético direto no extraído, já em UTF-8.

    O que o silver lê é sempre Parquet gerado pelo conversor de produção, e não um
    Parquet montado à mão que poderia divergir dele sem nada avisar.
    """
    destino = config.data_dir / "extraido" / config.competencia
    destino.mkdir(parents=True, exist_ok=True)
    with (destino / f"{nome}.csv").open("w", encoding="utf-8", newline="") as arquivo:
        escritor = csv.writer(
            arquivo, delimiter=";", quotechar='"', quoting=csv.QUOTE_ALL, lineterminator="\n"
        )
        for registro in registros:
            escritor.writerow([registro.get(coluna, "") for coluna in colunas])


def gravar_estabelecimentos(config: Config, registros: list[dict[str, str]]) -> None:
    _gravar_csv(config, "Estabelecimentos0", COLUNAS_ESTABELECIMENTOS, registros)
    converter_estabelecimentos(config)


def estabelecimento(
    cnpj_basico: str,
    *,
    uf: str = "SP",
    matriz: bool = True,
    ordem: str = "0001",
    dv: str = "00",
    situacao: str = "02",
) -> dict[str, str]:
    return {
        "cnpj_basico": cnpj_basico,
        "cnpj_ordem": ordem,
        "cnpj_dv": dv,
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


def test_desempate_cai_para_o_dv_quando_a_ordem_empata(tmp_path: Path) -> None:
    """O caso que `cnpj_ordem` sozinho não decide.

    Na competência 2026-06 nenhum `cnpj_basico` repete a dupla, então a chave de
    uma coluna bastava — por medição, não por garantia. Aqui a dupla empata, e sem
    a segunda coluna qual situação sobrevive passaria a ser escolha do motor.
    """
    config = Config(competencia="2026-06", data_dir=tmp_path, uf_alvo="SP")
    gravar_estabelecimentos(
        config,
        [
            estabelecimento("11111111", ordem="0001", dv="98", situacao="08"),
            estabelecimento("11111111", ordem="0001", dv="11", situacao="04"),
        ],
    )

    recorte = aplicar_recorte_por_uf(config)

    assert recorte.matrizes_repetidas == 1
    assert ler_recorte(recorte.caminho) == [("11111111", "04", "SP")]


def test_desempate_cai_para_a_situacao_quando_ordem_e_dv_empatam(tmp_path: Path) -> None:
    """O último degrau da ordem total.

    Se `cnpj_ordem` e `cnpj_dv` empatam, o critério passa a ser o próprio valor
    escolhido. Empatando também nele, todos os candidatos são iguais e a resposta
    é única por definição — não sobra caso indeterminado.
    """
    config = Config(competencia="2026-06", data_dir=tmp_path, uf_alvo="SP")
    gravar_estabelecimentos(
        config,
        [
            estabelecimento("11111111", ordem="0001", dv="98", situacao="08"),
            estabelecimento("11111111", ordem="0001", dv="98", situacao="02"),
        ],
    )

    recorte = aplicar_recorte_por_uf(config)

    assert recorte.matrizes_repetidas == 1
    assert ler_recorte(recorte.caminho) == [("11111111", "02", "SP")]


def test_desempate_com_ordem_distinta_continua_pela_ordem(tmp_path: Path) -> None:
    """A extensão só acrescenta degraus: onde a ordem já decidia, ela decide.

    Sem isto, a chave nova poderia ter passado a escolher pela menor situação
    também quando `cnpj_ordem` diverge — e o caso real de 08314885 mudaria de
    resposta sem ninguém pedir.
    """
    config = Config(competencia="2026-06", data_dir=tmp_path, uf_alvo="SP")
    gravar_estabelecimentos(
        config,
        [
            estabelecimento("08314885", ordem="0047", dv="98", situacao="08"),
            estabelecimento("08314885", ordem="0051", dv="74", situacao="02"),
        ],
    )

    recorte = aplicar_recorte_por_uf(config)

    assert ler_recorte(recorte.caminho) == [("08314885", "08", "SP")]


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


# =========================================================== empresas tipadas


NATUREZAS_PADRAO = {
    "2135": "Empresário (Individual)",
    "2062": "Sociedade Empresária Limitada",
    "4120": "Produtor Rural (Pessoa Física)",
    "0000": "Natureza Jurídica não informada",
}

QUALIFICACOES_PADRAO = {
    "49": "Sócio-Administrador",
    "50": "Empresário",
    "59": "Produtor Rural",
    "00": "Não informada",
}


def gravar_empresas(config: Config, registros: list[dict[str, str]]) -> None:
    _gravar_csv(config, "Empresas0", COLUNAS_EMPRESAS, registros)
    converter_empresas(config)


def _gravar_dominio(config: Config, prefixo: str, descricoes: dict[str, str]) -> None:
    _gravar_csv(
        config,
        prefixo,
        COLUNAS_DOMINIO,
        [{"codigo": codigo, "descricao": texto} for codigo, texto in descricoes.items()],
    )
    converter_tabela(
        config,
        Tabela(prefixo.lower(), prefixo, COLUNAS_DOMINIO),
        validacoes=VALIDACOES_DE_DOMINIO,
    )


def empresa(
    cnpj_basico: str,
    *,
    razao_social: str = "EMPRESA DE TESTE LTDA",
    natureza: str = "2062",
    qualificacao: str = "49",
    capital: str = "1000,00",
    porte: str = "01",
    ente: str = "",
) -> dict[str, str]:
    return {
        "cnpj_basico": cnpj_basico,
        "razao_social": razao_social,
        "natureza_juridica": natureza,
        "qualificacao_do_responsavel": qualificacao,
        "capital_social": capital,
        "porte": porte,
        "ente_federativo_responsavel": ente,
    }


def preparar_empresas(
    config: Config,
    registros: list[dict[str, str]],
    *,
    naturezas: dict[str, str] | None = None,
    qualificacoes: dict[str, str] | None = None,
    no_recorte: list[str] | None = None,
) -> None:
    """Monta o bronze e o recorte de que a tipagem depende.

    `no_recorte` existe para o caso em que o recorte e Empresas discordam — que é
    justamente o que a tipagem precisa recusar.
    """
    cnpjs = no_recorte if no_recorte is not None else [r["cnpj_basico"] for r in registros]
    gravar_estabelecimentos(config, [estabelecimento(c) for c in dict.fromkeys(cnpjs)])
    aplicar_recorte_por_uf(config)
    gravar_empresas(config, registros)
    _gravar_dominio(config, "Naturezas", naturezas or NATUREZAS_PADRAO)
    _gravar_dominio(config, "Qualificacoes", qualificacoes or QUALIFICACOES_PADRAO)


def ler_empresas(caminho: Path) -> list[dict[str, object]]:
    with duckdb.connect() as conexao:
        conexao.execute(f"CREATE VIEW e AS SELECT * FROM read_parquet('{caminho.as_posix()}')")
        colunas = [linha[0] for linha in conexao.execute("DESCRIBE e").fetchall()]
        return [
            dict(zip(colunas, linha, strict=True))
            for linha in conexao.execute("SELECT * FROM e ORDER BY cnpj_basico").fetchall()
        ]


@pytest.fixture
def config_de_silver(tmp_path: Path) -> Config:
    return Config(competencia="2026-06", data_dir=tmp_path, uf_alvo="SP")


# --------------------------------------------------- supressão de documento


def test_cpf_de_onze_digitos_sai_da_razao_social(config_de_silver: Config) -> None:
    """26,28% do recorte de SP tem esta forma. É o vazamento de volume da fonte."""
    preparar_empresas(
        config_de_silver,
        [empresa("11111111", razao_social="JOAO DA SILVA 12345678901", natureza="2135")],
    )

    resultado = tipar_empresas(config_de_silver)

    (linha,) = ler_empresas(resultado.caminho)
    assert linha["razao_social"] == f"JOAO DA SILVA {MARCA_DE_SUPRESSAO}"
    assert resultado.razoes_sociais_suprimidas == 1


@pytest.mark.parametrize("documento", ["123.456.789-01", "123.456.789.01", "123 456 789 01"])
def test_cpf_pontuado_sai_nas_variantes_que_a_fonte_tem(
    config_de_silver: Config, documento: str
) -> None:
    """Nenhuma destas é alcançada por regra de dígito corrido: a pontuação quebra a
    sequência. Foram 42 registros no recorte, e 42 vazamentos são 42 pessoas."""
    preparar_empresas(
        config_de_silver,
        [empresa("11111111", razao_social=f"MARIA SOUZA {documento}", natureza="2135")],
    )

    resultado = tipar_empresas(config_de_silver)

    (linha,) = ler_empresas(resultado.caminho)
    assert linha["razao_social"] == f"MARIA SOUZA {MARCA_DE_SUPRESSAO}"


def test_sequencia_de_dez_digitos_tambem_sai(config_de_silver: Config) -> None:
    """A decisão que a leitura do arquivo mudou.

    A regra prevista cobria onze dígitos, o comprimento do CPF. O arquivo trouxe
    `LUIZ FIRMINO DA SILVA 6677354881` — nome de pessoa e dez dígitos, que é um
    CPF cujo zero à esquerda se perdeu. Parar em onze publicaria esse CPF.
    """
    preparar_empresas(
        config_de_silver,
        [empresa("11111111", razao_social="LUIZ FIRMINO DA SILVA 6677354881", natureza="4120")],
    )

    resultado = tipar_empresas(config_de_silver)

    (linha,) = ler_empresas(resultado.caminho)
    assert linha["razao_social"] == f"LUIZ FIRMINO DA SILVA {MARCA_DE_SUPRESSAO}"


def test_cnpj_de_quatorze_digitos_tambem_sai(config_de_silver: Config) -> None:
    """Sobre-supressão deliberada: são 5 registros, o CNPJ é público e já existe
    como coluna própria. Separar o caso só acrescentaria caminho para errar."""
    preparar_empresas(config_de_silver, [empresa("11111111", razao_social="58254003000131 LTDA")])

    resultado = tipar_empresas(config_de_silver)

    (linha,) = ler_empresas(resultado.caminho)
    assert linha["razao_social"] == f"{MARCA_DE_SUPRESSAO} LTDA"


def test_sequencia_de_nove_digitos_permanece(config_de_silver: Config) -> None:
    """O outro lado da fronteira, sem o qual o teste acima só provaria que a regra
    apaga tudo. Nove dígitos não são CPF, e a supressão precisa saber parar."""
    preparar_empresas(
        config_de_silver,
        [empresa("11111111", razao_social="TRANSPORTADORA 123456789 LTDA")],
    )

    resultado = tipar_empresas(config_de_silver)

    (linha,) = ler_empresas(resultado.caminho)
    assert linha["razao_social"] == "TRANSPORTADORA 123456789 LTDA"
    assert resultado.razoes_sociais_suprimidas == 0


def test_cpf_partido_por_separador_sai(config_de_silver: Config) -> None:
    """O CPF canônico sem os pontos: nove dígitos, separador, dois verificadores.

    Regra de comprimento não alcança isto — o separador parte a sequência em nove
    e dois. São 6.518 registros escapando em Empresas, e a fonte escreve a palavra
    "CPF" ao lado em boa parte deles.
    """
    preparar_empresas(
        config_de_silver,
        [
            empresa("11111111", razao_social="GETULIO SOARES CRUZ CPF 177495146-00"),
            empresa("22222222", razao_social="LAZARO FREITAS C P F 170347796 00"),
        ],
    )

    resultado = tipar_empresas(config_de_silver)

    linhas = ler_empresas(resultado.caminho)
    assert linhas[0]["razao_social"] == f"GETULIO SOARES CRUZ CPF {MARCA_DE_SUPRESSAO}"
    assert linhas[1]["razao_social"] == f"LAZARO FREITAS C P F {MARCA_DE_SUPRESSAO}"
    assert resultado.razoes_sociais_suprimidas == 2


def test_nove_mais_dois_que_nao_valida_como_cpf_permanece(config_de_silver: Config) -> None:
    """A supressão é condicionada ao dígito verificador, não à forma.

    Sem este teste, a regra acima poderia estar apagando tudo que tem a forma
    `DDDDDDDDD-DD` — e a diferença entre suprimir o que é documento e suprimir o
    que se parece com um é justamente o que o verificador decide.
    """
    preparar_empresas(
        config_de_silver, [empresa("11111111", razao_social="CONTRATO 123456789-99 LTDA")]
    )

    resultado = tipar_empresas(config_de_silver)

    (linha,) = ler_empresas(resultado.caminho)
    assert linha["razao_social"] == "CONTRATO 123456789-99 LTDA"
    assert resultado.razoes_sociais_suprimidas == 0


def test_cpf_encurtado_de_nove_digitos_sai(config_de_silver: Config) -> None:
    """CPF que perdeu dois zeros à esquerda passa por baixo do limiar de dez.

    `VANDERLEI LORO 886812895` é `00886812895`, e é o único caso do recorte de SP.
    """
    preparar_empresas(
        config_de_silver, [empresa("11111111", razao_social="VANDERLEI LORO 886812895")]
    )

    resultado = tipar_empresas(config_de_silver)

    (linha,) = ler_empresas(resultado.caminho)
    assert linha["razao_social"] == f"VANDERLEI LORO {MARCA_DE_SUPRESSAO}"
    assert resultado.razoes_sociais_suprimidas == 1


def test_macro_de_cpf_concorda_com_o_verificador_das_fixtures(tmp_path: Path) -> None:
    """Duas implementações do mesmo algoritmo divergem no commit em que ninguém olha.

    A validação vive em SQL porque um UDF seria chamado 19,7 milhões de vezes. A
    de `test_fixtures` vive em Python porque a fixture é gerada fora do motor. Este
    teste é o que impede as duas de se separarem em silêncio.
    """
    casos = [
        "11144477735",
        "11144477736",
        "11111111111",
        "17749514600",
        "00886812895",
        "06677354881",
        "00123456789",
        "12345678999",
        "12345678901",
        "0011144477",
        "abcdefghijk",
        "",
    ]

    with duckdb.connect() as conexao:
        definir_macros(conexao)
        for numero in casos:
            obtido = conexao.execute("SELECT cpf_valido(?)", [numero]).fetchone()
            assert obtido is not None
            assert bool(obtido[0]) == cpf_valido(numero), numero

    assert any(cpf_valido(numero) for numero in casos), "o caso de teste precisa ter CPF válido"


def test_nenhum_padrao_de_documento_sobrevive_no_artefato(config_de_silver: Config) -> None:
    """Não confie em ter aplicado a supressão; varra o resultado e prove."""
    preparar_empresas(
        config_de_silver,
        [
            empresa("11111111", razao_social="JOAO DA SILVA 12345678901", natureza="2135"),
            empresa("22222222", razao_social="MARIA SOUZA 123.456.789-01", natureza="2135"),
            empresa("33333333", razao_social="ANA LIMA 6677354881", natureza="4120"),
            empresa("44444444", razao_social="JOSE CRUZ CPF 177495146-00", natureza="2135"),
            empresa("55555555", razao_social="PEDRO ALVES 886812895", natureza="4120"),
        ],
    )

    resultado = tipar_empresas(config_de_silver)

    with duckdb.connect() as conexao:
        for padrao in PADROES_DE_DOCUMENTO:
            sobreviventes = conexao.execute(
                f"SELECT count(*) FROM read_parquet('{resultado.caminho.as_posix()}') "
                "WHERE razao_social IS NOT NULL AND regexp_matches(razao_social, ?)",
                [padrao],
            ).fetchone()
            assert sobreviventes is not None and sobreviventes[0] == 0, padrao

        # Os condicionais não podem ser varridos pela forma: `123456789-99` tem a
        # forma e não é documento. O critério é o mesmo da regra — o verificador.
        restantes = conexao.execute(
            f"SELECT unnest(regexp_extract_all(razao_social, '{PADRAO_DE_CPF_PARTIDO}')) "
            f"FROM read_parquet('{resultado.caminho.as_posix()}') WHERE razao_social IS NOT NULL"
        ).fetchall()
        assert not [achado for (achado,) in restantes if cpf_valido(achado[:9] + achado[10:])]

        soltos = conexao.execute(
            f"SELECT unnest(regexp_extract_all(razao_social, '[0-9]+')) "
            f"FROM read_parquet('{resultado.caminho.as_posix()}') WHERE razao_social IS NOT NULL"
        ).fetchall()
        assert not [digitos for (digitos,) in soltos if cpf_valido(digitos.rjust(11, "0"))]

    assert resultado.razoes_sociais_suprimidas == 5


def test_razao_social_nula_nao_quebra_a_supressao(config_de_silver: Config) -> None:
    """A linha-toco da fonte tem razão social nula. Ela existe no dado real."""
    preparar_empresas(config_de_silver, [empresa("11111111", razao_social="")])

    resultado = tipar_empresas(config_de_silver)

    (linha,) = ler_empresas(resultado.caminho)
    assert linha["razao_social"] is None
    assert resultado.razoes_sociais_suprimidas == 0


# ------------------------------------------------------------------ tipagem


def test_capital_social_vira_decimal(config_de_silver: Config) -> None:
    preparar_empresas(
        config_de_silver,
        [
            empresa("11111111", capital="5000,00"),
            empresa("22222222", capital="0000000000000,00"),
            empresa("33333333", capital="999999999999,00"),
        ],
    )

    resultado = tipar_empresas(config_de_silver)

    linhas = ler_empresas(resultado.caminho)
    assert [linha["capital_social"] for linha in linhas] == [
        Decimal("5000.00"),
        Decimal("0.00"),
        Decimal("999999999999.00"),
    ]


def test_capital_social_malformado_e_recusado(config_de_silver: Config) -> None:
    """TRY_CAST devolveria nulo aqui, e capital nulo é indistinguível de zero."""
    preparar_empresas(config_de_silver, [empresa("11111111", capital="5.000,00")])

    with pytest.raises(CapitalSocialMalformadoError, match="silêncio"):
        tipar_empresas(config_de_silver)


def test_natureza_juridica_e_decodificada(config_de_silver: Config) -> None:
    preparar_empresas(config_de_silver, [empresa("11111111", natureza="2135")])

    resultado = tipar_empresas(config_de_silver)

    (linha,) = ler_empresas(resultado.caminho)
    assert linha["natureza_juridica"] == "2135"
    assert linha["natureza_juridica_descricao"] == "Empresário (Individual)"
    assert resultado.natureza_sem_descricao == 0


def test_qualificacao_do_responsavel_e_decodificada(config_de_silver: Config) -> None:
    """Mesma tabela e mesmo join da natureza: decodificar uma e deixar a outra em
    código só gera pergunta em revisão."""
    preparar_empresas(config_de_silver, [empresa("11111111", qualificacao="49")])

    resultado = tipar_empresas(config_de_silver)

    (linha,) = ler_empresas(resultado.caminho)
    assert linha["qualificacao_do_responsavel"] == "49"
    assert linha["qualificacao_do_responsavel_descricao"] == "Sócio-Administrador"
    assert resultado.qualificacao_sem_descricao == 0


def test_qualificacao_sem_correspondencia_nao_perde_o_registro(config_de_silver: Config) -> None:
    preparar_empresas(
        config_de_silver,
        [empresa("11111111", qualificacao="97")],
        qualificacoes={"49": "Sócio-Administrador"},
    )

    resultado = tipar_empresas(config_de_silver)

    (linha,) = ler_empresas(resultado.caminho)
    assert resultado.registros == 1
    assert linha["qualificacao_do_responsavel_descricao"] is None
    assert resultado.qualificacao_sem_descricao == 1


def test_natureza_sem_correspondencia_nao_perde_o_registro(config_de_silver: Config) -> None:
    """Join que descarta é como o recorte encolhe sem ninguém pedir."""
    preparar_empresas(
        config_de_silver,
        [empresa("11111111", natureza="9999"), empresa("22222222", natureza="2062")],
        naturezas={"2062": "Sociedade Empresária Limitada"},
    )

    resultado = tipar_empresas(config_de_silver)

    linhas = ler_empresas(resultado.caminho)
    assert resultado.registros == 2
    assert [linha["cnpj_basico"] for linha in linhas] == ["11111111", "22222222"]
    assert linhas[0]["natureza_juridica_descricao"] is None
    assert resultado.natureza_sem_descricao == 1


@pytest.mark.parametrize(
    ("codigo", "esperado"),
    [
        ("01", "Micro empresa"),
        ("03", "Empresa de pequeno porte"),
        ("05", "Demais"),
        ("", "Não informado"),
    ],
)
def test_porte_e_decodificado(config_de_silver: Config, codigo: str, esperado: str) -> None:
    preparar_empresas(config_de_silver, [empresa("11111111", porte=codigo)])

    resultado = tipar_empresas(config_de_silver)

    (linha,) = ler_empresas(resultado.caminho)
    assert linha["porte_descricao"] == esperado
    assert resultado.porte_sem_descricao == 0


def test_porte_desconhecido_fica_sem_descricao_e_e_contado(config_de_silver: Config) -> None:
    """`02` e `04` o PDF não define. Inventar significado é pior que devolver nulo."""
    preparar_empresas(config_de_silver, [empresa("11111111", porte="02")])

    resultado = tipar_empresas(config_de_silver)

    (linha,) = ler_empresas(resultado.caminho)
    assert linha["porte"] == "02"
    assert linha["porte_descricao"] is None
    assert resultado.porte_sem_descricao == 1


# ------------------------------------------------------- chave e integridade


def test_cnpj_basico_repetido_colapsa_preferindo_a_linha_com_mais_dados(
    config_de_silver: Config,
) -> None:
    """O caso real de 08314885: uma linha com dado e um toco vazio.

    Escolher pelo tamanho do conteúdo não é elegância — é a diferença entre o
    artefato ficar com 'FLAVIO PAVAO DE SOUZA' ou com uma razão social nula.
    """
    preparar_empresas(
        config_de_silver,
        [
            empresa(
                "08314885",
                razao_social="FLAVIO PAVAO DE SOUZA",
                natureza="4120",
                qualificacao="59",
                capital="0,00",
                porte="05",
            ),
            empresa(
                "08314885",
                razao_social="",
                natureza="0000",
                qualificacao="00",
                capital="0,00",
                porte="",
            ),
        ],
    )

    resultado = tipar_empresas(config_de_silver)

    (linha,) = ler_empresas(resultado.caminho)
    assert resultado.registros == 1
    assert resultado.cnpj_basico_repetidos == 1
    assert linha["razao_social"] == "FLAVIO PAVAO DE SOUZA"
    assert linha["natureza_juridica"] == "4120"


def test_empresa_do_recorte_ausente_e_recusada(config_de_silver: Config) -> None:
    """O recorte define o universo. Empresa que sai dele sem aviso o encolhe."""
    preparar_empresas(config_de_silver, [empresa("11111111")], no_recorte=["11111111", "22222222"])

    with pytest.raises(EmpresaAusenteError, match="universo"):
        tipar_empresas(config_de_silver)


def test_registros_batem_com_o_recorte(config_de_silver: Config) -> None:
    preparar_empresas(
        config_de_silver, [empresa("11111111"), empresa("22222222"), empresa("33333333")]
    )
    recorte = config_de_silver.data_dir / "silver" / "2026-06" / "recorte.parquet"

    resultado = tipar_empresas(config_de_silver)

    with duckdb.connect() as conexao:
        no_recorte = conexao.execute(
            f"SELECT count(*) FROM read_parquet('{recorte.as_posix()}')"
        ).fetchone()
    assert no_recorte is not None
    assert resultado.registros == no_recorte[0] == 3


def test_esquema_do_silver_e_exatamente_o_declarado(config_de_silver: Config) -> None:
    """Empresas não tem coluna de contato, e esta asserção é o que garante que
    nenhuma apareça: acrescentar coluna ao artefato publicado passa a exigir mudar
    o teste, que é onde a decisão fica visível."""
    preparar_empresas(config_de_silver, [empresa("11111111")])

    resultado = tipar_empresas(config_de_silver)

    with duckdb.connect() as conexao:
        descricao = conexao.execute(
            f"DESCRIBE SELECT * FROM read_parquet('{resultado.caminho.as_posix()}')"
        ).fetchall()
    assert tuple(coluna[0] for coluna in descricao) == COLUNAS_SILVER_EMPRESAS
    assert not {"correio_eletronico", "telefone_1", "ddd_1", "fax"} & {
        coluna[0] for coluna in descricao
    }


def test_empresas_e_o_mesmo_byte_a_byte_entre_execucoes(config_de_silver: Config) -> None:
    preparar_empresas(config_de_silver, [empresa("22222222"), empresa("11111111")])

    primeiro = tipar_empresas(config_de_silver).caminho.read_bytes()
    segundo = tipar_empresas(config_de_silver).caminho.read_bytes()

    assert primeiro == segundo


def test_tipagem_sem_recorte_e_recusada(config_de_silver: Config) -> None:
    gravar_estabelecimentos(config_de_silver, [estabelecimento("11111111")])
    gravar_empresas(config_de_silver, [empresa("11111111")])

    with pytest.raises(RecorteAusenteError, match="antes da tipagem"):
        tipar_empresas(config_de_silver)
