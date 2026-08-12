"""O portão de qualidade, exercitado contra casos construídos para reprovar.

Uma regra que nunca reprovou não provou que sabe reprovar. Cada verificação aqui
aparece duas vezes: uma com o silver íntegro, que ela precisa aceitar, e outra com
um defeito plantado, que ela precisa acusar — nomeando a regra, não só falhando.
"""

from __future__ import annotations

from pathlib import Path

import duckdb
import pytest

from grafo_societario.config import Config
from grafo_societario.transform.identity import gerar_identidades
from grafo_societario.transform.qualidade import (
    FORMATO_ESTRUTURADO,
    ErroDeQualidade,
    provar_que_a_varredura_acha,
    verificar_silver,
)
from grafo_societario.transform.silver import (
    aplicar_recorte_por_uf,
    tipar_empresas,
    tipar_socios,
)
from test_silver import (
    NATUREZAS_PADRAO,
    PAISES_PADRAO,
    QUALIFICACOES_PADRAO,
    _gravar_dominio,
    empresa,
    estabelecimento,
    gravar_empresas,
    gravar_estabelecimentos,
    gravar_socios,
    socio,
)

COM_DOCUMENTO = "JOAO DA SILVA 12345678901"
"""Uma razão social com CPF, para o bronze da fixture ter o que o controle
positivo precisa achar."""


@pytest.fixture
def silver(tmp_path: Path) -> Config:
    """Uma camada silver inteira e íntegra: recorte, empresas, sócios, identidades."""
    config = Config(competencia="2026-06", data_dir=tmp_path, uf_alvo="SP")
    cnpjs = ["11111111", "22222222", "33333333"]
    gravar_estabelecimentos(config, [estabelecimento(cnpj) for cnpj in cnpjs])
    aplicar_recorte_por_uf(config)

    gravar_empresas(
        config,
        [
            empresa("11111111", razao_social=COM_DOCUMENTO, natureza="2135"),
            empresa("22222222", razao_social="ACME LTDA"),
            empresa("33333333", razao_social="BETA LTDA"),
        ],
    )
    _gravar_dominio(config, "Naturezas", NATUREZAS_PADRAO)
    _gravar_dominio(config, "Qualificacoes", QUALIFICACOES_PADRAO)
    _gravar_dominio(config, "Paises", PAISES_PADRAO)
    tipar_empresas(config)

    gravar_socios(
        config,
        [
            socio("11111111", nome="MARIA SOUZA", documento="***123458**"),
            socio("22222222", nome="MARIA SOUZA", documento="***123458**"),
            socio("22222222", tipo="1", nome="HOLDING SA", documento="33333333000199"),
            socio("33333333", tipo="3", nome="JOHN SMITH", documento="", pais="249"),
        ],
    )
    tipar_socios(config)
    gerar_identidades(config)
    return config


def artefato(config: Config, nome: str) -> Path:
    return config.data_dir / "silver" / "2026-06" / f"{nome}.parquet"


def reescrever(caminho: Path, consulta: str) -> None:
    """Reescreve um artefato a partir de `t`, que é ele mesmo. Planta o defeito."""
    with duckdb.connect() as conexao:
        conexao.execute(f"CREATE TABLE t AS SELECT * FROM read_parquet('{caminho.as_posix()}')")
        conexao.execute(f"COPY ({consulta}) TO '{caminho.as_posix()}' (FORMAT PARQUET)")


def reprovacoes(config: Config) -> str:
    with pytest.raises(ErroDeQualidade) as erro:
        verificar_silver(config)
    return str(erro.value)


# ------------------------------------------------------------ controle positivo


def test_silver_integro_passa(silver: Config) -> None:
    """Sem isto, todas as regras abaixo poderiam estar simplesmente reprovando tudo."""
    relatorio = verificar_silver(silver)

    assert relatorio.achados == ()
    assert relatorio.regras >= 10


def test_a_varredura_prova_que_sabe_achar(silver: Config) -> None:
    """O bronze tem documento; se a varredura não o achar, ela está quebrada e o
    zero que devolve sobre o silver não significa nada."""
    assert provar_que_a_varredura_acha(silver, "2026-06") >= 1


def test_varredura_que_nao_acha_no_bronze_e_recusada(tmp_path: Path) -> None:
    """O outro lado do controle: bronze sem documento nenhum reprova o instrumento."""
    config = Config(competencia="2026-06", data_dir=tmp_path, uf_alvo="SP")
    gravar_estabelecimentos(config, [estabelecimento("11111111")])
    gravar_empresas(config, [empresa("11111111", razao_social="ACME LTDA")])

    with pytest.raises(ErroDeQualidade, match="quebrada"):
        provar_que_a_varredura_acha(config, "2026-06")


# ----------------------------------------------------------- dado pessoal


def test_coluna_de_contato_no_silver_reprova(silver: Config) -> None:
    """O compromisso do commit 17, agora afirmado em vez de pretendido."""
    reescrever(artefato(silver, "empresas"), "SELECT *, 'a@b.com' AS correio_eletronico FROM t")

    assert "[contato_no_silver]" in reprovacoes(silver)
    assert "correio_eletronico" in reprovacoes(silver)


@pytest.mark.parametrize(
    "documento",
    ["12345678901", "123.456.789-01", "177495146-00", "6677354881"],
)
def test_documento_solto_em_texto_livre_reprova(silver: Config, documento: str) -> None:
    """As três cláusulas do commit 16, agora como portão do artefato."""
    reescrever(
        artefato(silver, "empresas"),
        f"SELECT * REPLACE ('PEDRO ALVES {documento}' AS razao_social) FROM t",
    )

    assert "[documento_no_silver]" in reprovacoes(silver)


def test_cpf_sem_mascara_em_coluna_estruturada_reprova(silver: Config) -> None:
    """A isenção da varredura é mais estrita, não mais frouxa: onze dígitos
    corridos não casam com máscara nem com CNPJ."""
    reescrever(
        artefato(silver, "socios"),
        "SELECT * REPLACE ('12345678901' AS cnpj_cpf_socio) FROM t",
    )

    achados = reprovacoes(silver)
    assert "[forma_estruturada_violada]" in achados
    assert "cnpj_cpf_socio" in achados


def test_forma_declarada_frouxa_reprova(silver: Config, monkeypatch: pytest.MonkeyPatch) -> None:
    """O buraco que a isenção poderia abrir, fechado.

    Bastaria declarar `.*` para tirar uma coluna da varredura sem nada acusar.
    Esta regra olha a tabela de formas, e não o dado: é verificação do
    instrumento.
    """
    monkeypatch.setitem(FORMATO_ESTRUTURADO, "razao_social", ".*")

    assert "[forma_declarada_frouxa]" in reprovacoes(silver)


# -------------------------------------------------------- cadeia e chaves


def test_cadeia_quebrada_entre_recorte_e_empresas_reprova(silver: Config) -> None:
    """Cada etapa já confere o que produz. Isto confere se elas concordam."""
    reescrever(artefato(silver, "empresas"), "SELECT * FROM t WHERE cnpj_basico <> '33333333'")

    assert "[cadeia_recorte_empresas]" in reprovacoes(silver)


def test_cadeia_quebrada_entre_socios_e_identidades_reprova(silver: Config) -> None:
    reescrever(
        artefato(silver, "identidades"),
        "SELECT * REPLACE (vinculos_no_recorte + 1 AS vinculos_no_recorte) FROM t",
    )

    assert "[cadeia_socios_identidades]" in reprovacoes(silver)


def test_socio_sem_empresa_reprova(silver: Config) -> None:
    """Chave órfã aqui vira aresta pendurada na Fase 4, longe da causa."""
    reescrever(
        artefato(silver, "socios"),
        "SELECT * REPLACE ('99999999' AS cnpj_basico) FROM t WHERE cnpj_basico = '11111111' "
        "UNION ALL SELECT * FROM t WHERE cnpj_basico <> '11111111'",
    )

    assert "[socio_sem_empresa]" in reprovacoes(silver)


def test_identidade_que_nao_corresponde_a_socios_reprova(silver: Config) -> None:
    """Denuncia identidades geradas de um sócios anterior — o artefato velho que
    continua no lugar depois de a etapa de trás ser regerada."""
    reescrever(
        artefato(silver, "identidades"),
        "SELECT * REPLACE (CASE WHEN identificador = (SELECT min(identificador) FROM t) "
        "THEN '0000000000000000' ELSE identificador END AS identificador) FROM t",
    )

    assert "[vinculo_sem_identidade]" in reprovacoes(silver)


def test_chave_repetida_reprova(silver: Config) -> None:
    """Chave repetida não faz join falhar — faz multiplicar linha."""
    reescrever(
        artefato(silver, "identidades"),
        "(SELECT * FROM t) UNION ALL (SELECT * FROM t ORDER BY identificador LIMIT 1)",
    )

    assert "[chave_repetida]" in reprovacoes(silver)


# ------------------------------------------------------- nulos, faixas, vazio


def test_nulo_em_coluna_obrigatoria_reprova(silver: Config) -> None:
    reescrever(
        artefato(silver, "recorte"), "SELECT * REPLACE (NULL::VARCHAR AS situacao_cadastral) FROM t"
    )

    assert "[nulo_em_coluna_obrigatoria]" in reprovacoes(silver)


def test_valor_fora_de_faixa_reprova(silver: Config) -> None:
    """Valor fora de faixa não quebra nada — produz resultado plausível."""
    reescrever(
        artefato(silver, "empresas"),
        "SELECT * REPLACE (-1::DECIMAL(18,2) AS capital_social) FROM t",
    )

    assert "[fora_de_faixa]" in reprovacoes(silver)


def test_artefato_vazio_reprova(silver: Config) -> None:
    """Vazio faz toda etapa seguinte produzir vazio sem erro nenhum."""
    reescrever(artefato(silver, "identidades"), "SELECT * FROM t WHERE false")

    assert "[artefato_vazio]" in reprovacoes(silver)


def test_artefato_faltando_diz_o_que_falta(silver: Config) -> None:
    artefato(silver, "identidades").unlink()

    with pytest.raises(ErroDeQualidade, match="identidades"):
        verificar_silver(silver)


def test_a_mensagem_lista_todas_as_regras_quebradas(silver: Config) -> None:
    """Reprovar na primeira e parar obrigaria a rodar o pipeline uma vez por
    defeito. O relatório sai inteiro."""
    reescrever(
        artefato(silver, "empresas"),
        "SELECT * REPLACE (-1::DECIMAL(18,2) AS capital_social, "
        "'PEDRO ALVES 12345678901' AS razao_social) FROM t",
    )

    achados = reprovacoes(silver)
    assert "[fora_de_faixa]" in achados
    assert "[documento_no_silver]" in achados
