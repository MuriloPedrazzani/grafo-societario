"""Portão de qualidade da camada silver: as regras que o pipeline recusa quebrar.

Estas verificações rodam sobre os **artefatos em disco**, não sobre variáveis em
memória. Cada etapa já confere o que produz; o que falta é alguém perguntar se as
quatro tabelas continuam concordando entre si depois de todas terem sido geradas.

É o tipo de regressão que passa por todas as outras conferências: uma mudança que
altera duas etapas de forma coerente e errada mantém cada contagem individual de
pé e quebra só a relação entre elas.

**Nada aqui é aviso.** Uma regra quebrada levanta exceção e o pipeline para, com
código de saída diferente de zero. Artefato que reprovou não deve chegar à Fase 4,
porque lá a chave órfã vira aresta pendurada e o sintoma aparece longe da causa.

**Todo instrumento tem controle positivo.** A varredura de documento é apontada
para o bronze antes de valer: se ela não achar nada lá, onde sabidamente há 5,2
milhões de CPF, então ela não sabe achar, e o zero que ela devolve sobre o silver
não significa nada. Um medidor que só sabe retornar zero não mediu.
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Final

import duckdb

from grafo_societario.config import Config
from grafo_societario.transform.bronze import abrir_conexao
from grafo_societario.transform.identity import (
    consulta_de_socios_identificados,
    instalar_identificador,
)
from grafo_societario.transform.silver import definir_macros

logger = logging.getLogger(__name__)

COLUNAS_DE_CONTATO: Final = (
    "correio_eletronico",
    "ddd_1",
    "telefone_1",
    "ddd_2",
    "telefone_2",
    "ddd_fax",
    "fax",
)
"""As sete colunas que ficam no bronze e não atravessam.

87,8% dos estabelecimentos têm e-mail preenchido. A decisão de não trazê-los é do
projeto, mas decisão documentada é intenção — a asserção de esquema é o que a
torna verificável. Se alguém acrescentar uma delas ao silver, isto falha antes de
o artefato ser publicado, e não depois.
"""

ARTEFATOS: Final = ("recorte", "empresas", "socios", "identidades")

CHAVES: Final = {
    "recorte": "cnpj_basico",
    "empresas": "cnpj_basico",
    "identidades": "identificador",
}
"""Tabelas com chave única. `socios` não tem: um vínculo por linha, e a mesma
pessoa pode aparecer duas vezes na mesma empresa com qualificações diferentes."""

OBRIGATORIAS: Final = {
    "recorte": ("cnpj_basico", "situacao_cadastral", "uf"),
    "empresas": ("cnpj_basico",),
    "socios": ("cnpj_basico", "identificador_socio"),
    "identidades": ("identificador", "tipo", "confianca", "vinculos_no_recorte"),
}

FORMATO_ESTRUTURADO: Final = {
    "cnpj_cpf_socio": r"^(\*\*\*[0-9]{6}\*\*|[0-9]{14})$",
    "representante_legal": r"^\*\*\*[0-9]{6}\*\*$",
    "cpf_mascarado": r"^\*\*\*[0-9]{6}\*\*$",
    "cnpj_basico": r"^[0-9]{8}$",
    "identificador": r"^[0-9a-f]{16}$",
}
"""Colunas que guardam documento ou chave por contrato, e a forma exata de cada uma.

A varredura de texto livre não serve para elas, e apontá-la aqui produz falso
positivo: `cnpj_cpf_socio` guarda CNPJ de catorze dígitos porque é para isso que
ela existe, e `identificador` é hexadecimal, onde nove dígitos seguidos aparecem
por acaso em 3% dos hashes.

A saída **não** é isentá-las. É submetê-las a uma regra mais estrita: em vez de
"não pode ter documento solto", vale "só pode ter exatamente esta forma". Um CPF
sem máscara em `cnpj_cpf_socio` reprova aqui, porque onze dígitos corridos não
casam com nenhuma das duas formas permitidas.

O que sustenta o desenho é a regra de cobertura: **toda coluna de texto de todo
artefato passa por uma das duas verificações**, nunca por nenhuma. Sem essa
garantia, esta tabela seria uma lista de exceções — que é como se abre buraco sem
ninguém perceber.
"""


class ErroDeQualidade(RuntimeError):
    """A camada silver reprovou em ao menos uma regra."""


@dataclass(frozen=True)
class Achado:
    """Uma regra quebrada, com o suficiente para agir sem reabrir o dado."""

    regra: str
    detalhe: str

    def __str__(self) -> str:
        return f"[{self.regra}] {self.detalhe}"


@dataclass(frozen=True)
class Relatorio:
    regras: int
    achados: tuple[Achado, ...]


Regra = Callable[[duckdb.DuckDBPyConnection, dict[str, Path]], list[Achado]]


def _texto(conexao: duckdb.DuckDBPyConnection, caminho: Path) -> list[str]:
    """Colunas de texto do artefato, descobertas do esquema e não listadas à mão.

    Descobrir em vez de listar é o que faz uma coluna nova nascer coberta pela
    varredura. Lista escrita à mão protege o que existia no dia em que foi escrita.
    """
    return [
        str(linha[0])
        for linha in conexao.execute(
            f"DESCRIBE SELECT * FROM read_parquet('{caminho.as_posix()}')"
        ).fetchall()
        if str(linha[1]) == "VARCHAR"
    ]


def contato_nao_atravessa(
    conexao: duckdb.DuckDBPyConnection, artefatos: dict[str, Path]
) -> list[Achado]:
    """Nenhum artefato tem coluna de contato. Ausência afirmada, não pretendida."""
    achados = []
    for nome, caminho in artefatos.items():
        colunas = {
            str(linha[0])
            for linha in conexao.execute(
                f"DESCRIBE SELECT * FROM read_parquet('{caminho.as_posix()}')"
            ).fetchall()
        }
        presentes = sorted(colunas & set(COLUNAS_DE_CONTATO))
        if presentes:
            achados.append(
                Achado(
                    "contato_no_silver",
                    f"{nome}.parquet tem {', '.join(presentes)}. Contato de pessoa não "
                    "atravessa para o silver: ele fica no bronze, que é local e nunca "
                    "publicado.",
                )
            )
    return achados


def documento_foi_suprimido(
    conexao: duckdb.DuckDBPyConnection, artefatos: dict[str, Path]
) -> list[Achado]:
    """Varre toda coluna de texto de todo artefato à procura de documento.

    O critério é a própria regra de supressão: se aplicá-la mudaria o valor, é
    porque sobrou documento. Reusar a regra em vez de reescrever os padrões é o
    que impede a varredura de divergir daquilo que ela deveria conferir.

    A varredura é validada contra o bronze antes de valer sobre o silver.
    """
    achados = []
    for nome, caminho in artefatos.items():
        for coluna in _texto(conexao, caminho):
            if coluna in FORMATO_ESTRUTURADO:
                continue
            resultado = conexao.execute(
                f"SELECT count(*), min({coluna}) FROM read_parquet('{caminho.as_posix()}') "
                f"WHERE {coluna} IS NOT NULL AND suprimir_documentos({coluna}) <> {coluna}"
            ).fetchone()
            if resultado and int(resultado[0]):
                achados.append(
                    Achado(
                        "documento_no_silver",
                        f"{nome}.parquet tem {int(resultado[0]):,} valores com documento em "
                        f"{coluna}, o primeiro deles {resultado[1]!r}. Artefato do silver é "
                        "publicado em Release e em imagem; documento aqui é documento "
                        "publicado.",
                    )
                )
    return achados


def coluna_estruturada_tem_a_forma_declarada(
    conexao: duckdb.DuckDBPyConnection, artefatos: dict[str, Path]
) -> list[Achado]:
    """As colunas de documento e de chave, contra a forma exata que prometem.

    É a regra mais estrita das duas: um CPF sem máscara em `cnpj_cpf_socio` reprova
    aqui, porque onze dígitos corridos não casam com máscara nem com CNPJ.
    """
    achados = []
    for nome, caminho in artefatos.items():
        for coluna in _texto(conexao, caminho):
            forma = FORMATO_ESTRUTURADO.get(coluna)
            if forma is None:
                continue
            resultado = conexao.execute(
                f"SELECT count(*), min({coluna}) FROM read_parquet('{caminho.as_posix()}') "
                f"WHERE {coluna} IS NOT NULL AND NOT regexp_matches({coluna}, '{forma}')"
            ).fetchone()
            if resultado and int(resultado[0]):
                achados.append(
                    Achado(
                        "forma_estruturada_violada",
                        f"{nome}.parquet tem {int(resultado[0]):,} valores de {coluna} fora de "
                        f"{forma}, o primeiro deles {resultado[1]!r}.",
                    )
                )
    return achados


DOCUMENTOS_DE_PROVA: Final = (
    "12345678901",
    "123.456.789-01",
    "123 456 789 01",
    "177495146-00",
    "6677354881",
    "JOSE DA SILVA 12345678901",
)
"""Documentos que nenhuma forma declarada pode aceitar. As cinco primeiras são as
formas que a regra de supressão reconhece; a última é o caso de volume."""


def forma_declarada_recusa_documento(
    conexao: duckdb.DuckDBPyConnection, artefatos: dict[str, Path]
) -> list[Achado]:
    """Controle positivo da isenção: cada forma declarada precisa recusar um CPF.

    Tirar uma coluna da varredura de texto livre exige declarar a forma dela. Sem
    esta regra, bastaria declarar `.*` para calar uma reprovação — e a isenção,
    que existe para ser mais estrita, viraria um buraco.

    Isto não olha o dado; olha a própria tabela de formas. É a verificação do
    instrumento, não da medida.
    """
    achados = []
    for coluna, forma in FORMATO_ESTRUTURADO.items():
        for documento in DOCUMENTOS_DE_PROVA:
            aceita = conexao.execute("SELECT regexp_matches(?, ?)", [documento, forma]).fetchone()
            if aceita and bool(aceita[0]):
                achados.append(
                    Achado(
                        "forma_declarada_frouxa",
                        f"a forma de {coluna} aceita {documento!r}, que é documento. Isentar "
                        "uma coluna da varredura só vale se a forma declarada for mais "
                        "estrita que ela, nunca menos.",
                    )
                )
    return achados


def cadeia_de_conservacao(
    conexao: duckdb.DuckDBPyConnection, artefatos: dict[str, Path]
) -> list[Achado]:
    """As quatro contagens precisam concordar entre si, e não só consigo mesmas.

    O recorte define quantas empresas existem; empresas tem de ter exatamente
    essas; sócios são os vínculos delas; e a soma de `vinculos_no_recorte` das
    identidades tem de devolver o mesmo total de vínculos. Cada etapa já confere o
    que produz — esta é a única afirmação de que elas continuam falando da mesma
    coisa depois de todas terem rodado.
    """

    def quantos(nome: str, expressao: str = "count(*)") -> int:
        resultado = conexao.execute(
            f"SELECT {expressao} FROM read_parquet('{artefatos[nome].as_posix()}')"
        ).fetchone()
        return int(resultado[0]) if resultado and resultado[0] is not None else 0

    recorte, empresas = quantos("recorte"), quantos("empresas")
    socios = quantos("socios")
    vinculos = quantos("identidades", "sum(vinculos_no_recorte)")

    achados = []
    if recorte != empresas:
        achados.append(
            Achado(
                "cadeia_recorte_empresas",
                f"o recorte tem {recorte:,} empresas e a tabela de empresas tem {empresas:,}. "
                "O recorte define o universo, e empresas precisa cobri-lo exatamente.",
            )
        )
    if socios != vinculos:
        achados.append(
            Achado(
                "cadeia_socios_identidades",
                f"são {socios:,} vínculos em sócios e {vinculos:,} somados em identidades. "
                "Vínculo que não chega a uma identidade é aresta que some do grafo.",
            )
        )
    return achados


def integridade_referencial(
    conexao: duckdb.DuckDBPyConnection, artefatos: dict[str, Path]
) -> list[Achado]:
    """Toda chave de junção existe do outro lado, exatamente uma vez.

    A Fase 4 junta estas tabelas para montar aresta. Chave órfã aqui vira aresta
    pendurada lá, e o sintoma aparece a duas fases de distância da causa.
    """
    achados = []

    orfas = conexao.execute(
        f"SELECT count(*) FROM (SELECT DISTINCT cnpj_basico FROM "
        f"read_parquet('{artefatos['socios'].as_posix()}')) s "
        f"ANTI JOIN read_parquet('{artefatos['empresas'].as_posix()}') e USING (cnpj_basico)"
    ).fetchone()
    if orfas and int(orfas[0]):
        achados.append(
            Achado(
                "socio_sem_empresa",
                f"{int(orfas[0]):,} cnpj_basico de sócios não existem em empresas.",
            )
        )

    # Recomputa o identificador a partir do silver em disco e confere contra o
    # artefato em disco: é o que denuncia identidades geradas de um sócios antigo.
    conexao.execute(
        "CREATE OR REPLACE TEMP TABLE conferencia AS "
        + consulta_de_socios_identificados(artefatos["socios"])
    )
    sem_identidade = conexao.execute(
        f"""
        SELECT count(*) FROM (
          SELECT DISTINCT {_IDENTIFICADOR_DE_CONFERENCIA} AS identificador FROM conferencia
        ) c ANTI JOIN read_parquet('{artefatos["identidades"].as_posix()}') i
        USING (identificador)
        """
    ).fetchone()
    if sem_identidade and int(sem_identidade[0]):
        achados.append(
            Achado(
                "vinculo_sem_identidade",
                f"{int(sem_identidade[0]):,} identificadores derivados de sócios não existem "
                "em identidades. O artefato de identidades não corresponde ao de sócios: "
                "regere os dois na mesma execução.",
            )
        )
    return achados


def chave_unica(conexao: duckdb.DuckDBPyConnection, artefatos: dict[str, Path]) -> list[Achado]:
    """Chave repetida não faz join falhar — faz multiplicar linha."""
    achados = []
    for nome, chave in CHAVES.items():
        resultado = conexao.execute(
            f"SELECT count(*), count(DISTINCT {chave}) FROM "
            f"read_parquet('{artefatos[nome].as_posix()}')"
        ).fetchone()
        if resultado and int(resultado[0]) != int(resultado[1]):
            achados.append(
                Achado(
                    "chave_repetida",
                    f"{nome}.parquet tem {int(resultado[0]):,} linhas para "
                    f"{int(resultado[1]):,} valores de {chave}.",
                )
            )
    return achados


def sem_nulo_no_obrigatorio(
    conexao: duckdb.DuckDBPyConnection, artefatos: dict[str, Path]
) -> list[Achado]:
    achados = []
    for nome, colunas in OBRIGATORIAS.items():
        for coluna in colunas:
            resultado = conexao.execute(
                f"SELECT count(*) FROM read_parquet('{artefatos[nome].as_posix()}') "
                f"WHERE {coluna} IS NULL"
            ).fetchone()
            if resultado and int(resultado[0]):
                achados.append(
                    Achado(
                        "nulo_em_coluna_obrigatoria",
                        f"{nome}.parquet tem {int(resultado[0]):,} nulos em {coluna}.",
                    )
                )
    return achados


def faixas_plausiveis(
    conexao: duckdb.DuckDBPyConnection, artefatos: dict[str, Path]
) -> list[Achado]:
    """Valores fora de faixa não quebram nada — produzem resultado plausível."""
    limites = [
        ("empresas", "capital_social < 0", "capital social negativo"),
        ("identidades", "vinculos_no_recorte < 1", "identidade sem nenhum vínculo"),
        (
            "identidades",
            "taxa_de_colisao IS NOT NULL AND (taxa_de_colisao < 0 OR taxa_de_colisao > 1)",
            "taxa de colisão fora de [0, 1]",
        ),
        (
            "socios",
            "data_entrada_sociedade IS NOT NULL AND data_entrada_sociedade < DATE '1900-01-01'",
            "entrada em sociedade anterior a 1900",
        ),
    ]
    achados = []
    for nome, condicao, descricao in limites:
        resultado = conexao.execute(
            f"SELECT count(*) FROM read_parquet('{artefatos[nome].as_posix()}') WHERE {condicao}"
        ).fetchone()
        if resultado and int(resultado[0]):
            achados.append(
                Achado("fora_de_faixa", f"{nome}.parquet: {int(resultado[0]):,} com {descricao}.")
            )
    return achados


def contagem_minima(conexao: duckdb.DuckDBPyConnection, artefatos: dict[str, Path]) -> list[Achado]:
    """Artefato vazio faz toda etapa seguinte produzir vazio sem erro nenhum."""
    achados = []
    for nome, caminho in artefatos.items():
        resultado = conexao.execute(
            f"SELECT count(*) FROM read_parquet('{caminho.as_posix()}')"
        ).fetchone()
        if not resultado or not int(resultado[0]):
            achados.append(Achado("artefato_vazio", f"{nome}.parquet não tem nenhuma linha."))
    return achados


REGRAS: Final[tuple[Regra, ...]] = (
    contato_nao_atravessa,
    documento_foi_suprimido,
    coluna_estruturada_tem_a_forma_declarada,
    forma_declarada_recusa_documento,
    cadeia_de_conservacao,
    integridade_referencial,
    chave_unica,
    sem_nulo_no_obrigatorio,
    faixas_plausiveis,
    contagem_minima,
)

_IDENTIFICADOR_DE_CONFERENCIA: Final = """
CASE confianca
  WHEN 'exata' THEN identificador(['pessoa_juridica', substr(cnpj_cpf_socio, 1, 8)])
  WHEN 'estimada' THEN identificador(['pessoa_fisica', nome, cnpj_cpf_socio])
  WHEN 'fraca' THEN identificador(['estrangeiro', nome, coalesce(pais, '')])
  ELSE identificador(['nao_fundivel', cnpj_basico, coalesce(cnpj_cpf_socio, ''),
                      coalesce(nome, ''), coalesce(pais, '')])
END
"""


def provar_que_a_varredura_acha(config: Config, competencia: str) -> int:
    """Controle positivo: aponta a varredura para o bronze e exige que ela ache.

    O bronze tem 5,2 milhões de razões sociais com CPF sem máscara. Se a varredura
    não os encontrar, o zero que ela devolve sobre o silver não prova nada — e
    seria exatamente o resultado de um regex quebrado ou de uma macro não
    instalada.
    """
    entrada = config.data_dir / "bronze" / competencia
    partições = sorted(entrada.glob("empresas*.parquet"))
    if not partições:
        raise ErroDeQualidade(
            f"Não há bronze de empresas em {entrada} para validar a varredura. Sem controle "
            "positivo, uma varredura que devolve zero é indistinguível de uma quebrada."
        )

    with abrir_conexao(config, config.data_dir / "duckdb-tmp") as conexao:
        definir_macros(conexao)
        resultado = conexao.execute(
            f"SELECT count(*) FROM read_parquet('{(entrada / 'empresas0.parquet').as_posix()}') "
            "WHERE razao_social IS NOT NULL "
            "AND suprimir_documentos(razao_social) <> razao_social"
        ).fetchone()
    achados = int(resultado[0]) if resultado else 0
    if not achados:
        raise ErroDeQualidade(
            "A varredura de documento não achou nada no bronze, onde há milhões. Ela está "
            "quebrada, e qualquer zero que devolva sobre o silver é falso."
        )
    return achados


def verificar_silver(config: Config, competencia: str | None = None) -> Relatorio:
    """Roda todas as regras sobre os artefatos do silver. Levanta se alguma quebrar."""
    alvo = competencia or config.competencia
    silver = config.data_dir / "silver" / alvo
    artefatos = {nome: silver / f"{nome}.parquet" for nome in ARTEFATOS}

    faltando = sorted(nome for nome, caminho in artefatos.items() if not caminho.exists())
    if faltando:
        raise ErroDeQualidade(
            f"Faltam artefatos do silver em {silver}: {', '.join(faltando)}. A verificação "
            "compara as tabelas entre si e não tem o que comparar sem todas elas."
        )

    with abrir_conexao(config, config.data_dir / "duckdb-tmp") as conexao:
        definir_macros(conexao)
        instalar_identificador(conexao)
        achados = tuple(achado for regra in REGRAS for achado in regra(conexao, artefatos))

    logger.info(
        "silver verificado",
        extra={
            "competencia": alvo,
            "regras": len(REGRAS),
            "achados": len(achados),
            "reprovacoes": [str(achado) for achado in achados],
        },
    )
    if achados:
        raise ErroDeQualidade(
            f"A camada silver reprovou em {len(achados)} de {len(REGRAS)} regras:\n"
            + "\n".join(f"  {achado}" for achado in achados)
        )
    return Relatorio(regras=len(REGRAS), achados=achados)
