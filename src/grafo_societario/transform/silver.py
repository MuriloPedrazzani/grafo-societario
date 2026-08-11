"""Camada silver: o recorte territorial que define o universo do projeto.

O grafo inteiro do Brasil não cabe nas restrições deste projeto, então o recorte
é a primeira decisão que sobra em todas as outras. Ele é feito pela UF do
estabelecimento **matriz** — ver ADR-005 — e não pela UF de cada estabelecimento:
uma empresa é uma coisa só, e uma filial em outro estado não faz dela outra
empresa.

**A premissa do recorte foi medida, não presumida.** Recortar pela matriz só é
defensável se toda empresa tiver uma. Se houvesse `cnpj_basico` sem nenhum
registro de matriz, essas empresas não entrariam no recorte de UF alguma e
sumiriam do projeto em silêncio — modo de falha que nenhuma contagem por UF
revelaria. Medido na competência 2026-06: **zero** entre 68.629.147 `cnpj_basico`
de Estabelecimentos. A premissa se sustenta, e o número precisa ser refeito a
cada competência, não herdado desta.

**Situação cadastral não filtra nada aqui.** As baixadas são 33,8 milhões de
registros — o maior grupo do arquivo, à frente das ativas. Vínculo de empresa que
fechou continua sendo vínculo, e é exatamente o que interessa a quem investiga
sucessão de sócios. A coluna é preservada para que a API decida; o silver não
decide por ela.

**A unicidade é por construção, não por validação depois.** O recorte agrega por
`cnpj_basico`, então nenhum join adiante pode multiplicar linha — a garantia está
na forma da consulta. A validação que roda em seguida existe para provar que a
consulta faz o que esta frase afirma, e é guarda contra defeito meu, não contra
defeito da fonte.

**Matriz repetida existe no dado real e é contada em voz alta.** Um `cnpj_basico`
em 68,6 milhões tem duas matrizes na competência 2026-06, e o mesmo registro
aparece duas vezes em Empresas. É defeito da fonte, não do pipeline, e tratá-lo
como erro fatal impediria o projeto de processar o dado que existe. Ele colapsa
para uma linha, por uma ordem total que não deixa caso para o motor decidir — ver
`CHAVE_DE_DESEMPATE` —, e a quantidade de casos vai para o log de toda execução,
inclusive quando é zero. Colapso silencioso é o que não pode acontecer; colapso
contado é aritmética honesta.

**Todo join com tabela de domínio é LEFT, e os não-casados são contados.** Vale
para natureza jurídica, porte, qualificação, município, país e CNAE, sem exceção
e sem discussão caso a caso. A razão é medida, não suposta: o código `36` de
qualificação aparece em 782 empresas do recorte de SP e **não existe** entre as 68
linhas de `Qualificacoes`. As tabelas de decodificação da Receita não cobrem a
própria Receita. Um join interno teria apagado essas 782 empresas do projeto sem
erro nenhum; o LEFT as mantém sem descrição, e o contador põe o número no log de
toda execução.
"""

from __future__ import annotations

import calendar
import datetime as dt
import logging
from dataclasses import dataclass
from pathlib import Path
from typing import Final

import duckdb

from grafo_societario.config import Config
from grafo_societario.transform.bronze import abrir_conexao

logger = logging.getLogger(__name__)

MATRIZ: Final = "1"
"""Valor de `identificador_matriz_filial` que marca a matriz.

Conferido contra a competência inteira: a coluna tem exatamente `'1'` e `'2'`,
sem zero à esquerda. A coluna vizinha `situacao_cadastral` não tem essa sorte —
o PDF oficial lista `2` onde o arquivo traz `02` —, e é por isso que a conferência
foi feita em vez de deduzida do documento.
"""

CHAVE_DE_DESEMPATE: Final = "(cnpj_ordem, cnpj_dv, situacao_cadastral)"
"""Ordem **total** entre as matrizes de um mesmo `cnpj_basico`.

Na competência 2026-06 o único caso de matriz repetida tem `cnpj_ordem` distintos
— `0047` e `0051` —, e nenhum dos 71,8 milhões de registros repete a dupla. Ou
seja: hoje `cnpj_ordem` sozinho já decide.

Hoje. Isso é propriedade **medida desta competência**, não garantida pela fonte, e
a próxima não avisa quando quebrar. Com dois `cnpj_ordem` iguais o desempate por
uma coluna só não desempata nada, e qual `situacao_cadastral` sobrevive passa a
ser escolha do motor — podendo variar entre versões e entre execuções, o que
quebra a imutabilidade que a Fase 8 promete sobre o artefato.

A chave inclui `situacao_cadastral`, que é o próprio valor escolhido, e é isso
que a torna total: se `cnpj_ordem` e `cnpj_dv` empatarem, vence a menor situação;
se ela também empatar, todos os candidatos têm o mesmo valor e a resposta é única
por definição. Não sobra caso em que o motor decida.

Garantia por construção vence validação posterior — é o mesmo argumento que já
sustenta a unicidade do recorte, aplicado ao desempate.
"""


class ErroDeSilver(RuntimeError):
    """Falha ao construir a camada silver."""


class BronzeAusenteError(ErroDeSilver):
    """A competência pedida não tem camada bronze em disco."""


class RecorteVazioError(ErroDeSilver):
    """Nenhuma matriz na UF alvo."""


class RecorteAusenteError(ErroDeSilver):
    """A tipagem foi pedida antes de o recorte por UF existir."""


class CapitalSocialMalformadoError(ErroDeSilver):
    """Algum `capital_social` não casa com o formato da fonte."""


class EmpresaAusenteError(ErroDeSilver):
    """Um `cnpj_basico` do recorte não tem linha em Empresas."""


class VinculoPerdidoError(ErroDeSilver):
    """A tabela de sócios saiu com menos vínculos do que entrou."""


class CnpjBasicoDuplicadoError(ErroDeSilver):
    """O recorte saiu com `cnpj_basico` repetido."""


@dataclass(frozen=True)
class Recorte:
    """O que o recorte produziu, para quem chamou não precisar reconsultar."""

    caminho: Path

    uf: str
    """UF da matriz que define o recorte."""

    empresas: int
    """Quantos `cnpj_basico` entraram. É a medida do universo do projeto."""

    matrizes_repetidas: int
    """Quantos `cnpj_basico` tinham mais de uma matriz e colapsaram para uma
    linha. Zero é o valor esperado, e por isso mesmo ele é registrado: um número
    que só aparece quando incomoda não deixa saber quando estava tudo bem."""

    situacoes: tuple[tuple[str, int], ...]
    """Contagem por `situacao_cadastral`, do código para o total. Nenhuma delas
    filtra o recorte — a repartição é reportada para que a decisão de filtrar,
    que é da API, seja tomada sabendo o que ela custa."""


def _fonte_de_estabelecimentos(config: Config, competencia: str) -> str:
    return _fonte_do_bronze(config, competencia, "estabelecimentos")


def _fonte_de_empresas(config: Config, competencia: str) -> str:
    return _fonte_do_bronze(config, competencia, "empresas")


def _fonte_do_bronze(config: Config, competencia: str, tabela: str) -> str:
    """Cláusula de leitura das partições de uma tabela do bronze.

    A ausência é conferida aqui para que ela chegue como instrução do que fazer.
    Sem isto o DuckDB levanta uma `IOException` sobre um glob que não casou, e
    quem só esqueceu de rodar o bronze precisa deduzir isso de um erro de I/O.
    """
    entrada = config.data_dir / "bronze" / competencia
    padrao = entrada / f"{tabela}*.parquet"
    if not sorted(entrada.glob(padrao.name)):
        raise BronzeAusenteError(
            f"Nenhum Parquet de {tabela} em {entrada}. Rode o bronze desta "
            f"competência antes do silver."
        )
    return f"read_parquet('{padrao.as_posix()}')"


def validar_cnpj_basico_unico(conexao: duckdb.DuckDBPyConnection, caminho: Path) -> None:
    """Recusa recorte com `cnpj_basico` repetido.

    O recorte é a chave de junção de tudo o que vem depois: empresas, sócios e,
    na Fase 4, os nós do grafo. Uma chave repetida aqui não faz join nenhum
    falhar — faz cada um deles **multiplicar linha**, e o sintoma aparece como
    contagem de arestas inflada, fases adiante, sem nada apontando para cá.
    """
    duplicados = conexao.execute(
        f"SELECT cnpj_basico, count(*) AS quantas FROM read_parquet('{caminho.as_posix()}') "
        "GROUP BY cnpj_basico HAVING count(*) > 1 ORDER BY quantas DESC, cnpj_basico LIMIT 5"
    ).fetchall()
    if not duplicados:
        return

    amostra = ", ".join(f"{cnpj!r} aparece {quantas}x" for cnpj, quantas in duplicados)
    raise CnpjBasicoDuplicadoError(
        f"{caminho.name} tem cnpj_basico repetido: {amostra}. O recorte é a chave de junção "
        "de toda a camada silver, e chave repetida multiplica linha em cada join que a usar, "
        "aparecendo só muito depois como contagem de arestas inflada."
    )


def aplicar_recorte_por_uf(config: Config, competencia: str | None = None) -> Recorte:
    """Seleciona os `cnpj_basico` cuja matriz está na UF alvo.

    A saída carrega a UF em coluna própria. Ela é constante — custa alguns bytes
    depois da compressão por dicionário — e existe porque o caminho do arquivo
    não diz de qual UF ele é: rodar SP e depois RJ com o mesmo `DATA_DIR`
    sobrescreveria o primeiro, e um artefato que não sabe dizer o que é vira
    diagnóstico impossível três fases adiante.
    """
    alvo = competencia or config.competencia
    fonte = _fonte_de_estabelecimentos(config, alvo)

    destino_do_diretorio = config.data_dir / "silver" / alvo
    destino_do_diretorio.mkdir(parents=True, exist_ok=True)
    destino = destino_do_diretorio / "recorte.parquet"

    with abrir_conexao(config, config.data_dir / "duckdb-tmp") as conexao:
        # `min(uf)` sobre um conjunto já filtrado devolve a própria UF alvo. A
        # coluna sai do dado, e não de um literal interpolado na consulta.
        conexao.execute(
            f"CREATE OR REPLACE TEMP TABLE recorte AS "
            f"SELECT cnpj_basico, "
            f"       arg_min(situacao_cadastral, {CHAVE_DE_DESEMPATE}) AS situacao_cadastral, "
            f"       min(uf) AS uf, "
            f"       count(*) AS matrizes "
            f"FROM {fonte} "
            f"WHERE identificador_matriz_filial = ? AND uf = ? "
            f"GROUP BY cnpj_basico",
            [MATRIZ, config.uf_alvo],
        )

        totais = conexao.execute(
            "SELECT count(*), count(*) FILTER (WHERE matrizes > 1) FROM recorte"
        ).fetchone()
        empresas, repetidas = (int(totais[0]), int(totais[1])) if totais else (0, 0)

        if not empresas:
            raise RecorteVazioError(
                f"Nenhuma matriz em {config.uf_alvo} na competência {alvo}. O recorte vazio "
                "faria todas as etapas seguintes produzirem artefato vazio sem erro nenhum. "
                "Confira UF_ALVO e se o bronze desta competência foi gerado."
            )

        situacoes = tuple(
            (str(codigo), int(quantos))
            for codigo, quantos in conexao.execute(
                "SELECT situacao_cadastral, count(*) FROM recorte GROUP BY 1 ORDER BY 1"
            ).fetchall()
        )

        # A ordenação torna o artefato o mesmo byte a byte entre execuções. O grafo
        # da Fase 4 depende de índice determinístico, e determinismo é mais barato
        # de manter desde o primeiro artefato do que de reconquistar depois.
        parcial = destino.with_name(f"{destino.name}.parcial")
        conexao.execute(
            f"COPY (SELECT cnpj_basico, situacao_cadastral, uf FROM recorte "
            f"ORDER BY cnpj_basico) "
            f"TO '{parcial.as_posix()}' (FORMAT PARQUET, COMPRESSION ZSTD)"
        )

        try:
            validar_cnpj_basico_unico(conexao, parcial)
        except ErroDeSilver:
            parcial.unlink(missing_ok=True)
            raise

    parcial.replace(destino)

    logger.info(
        "recorte por UF aplicado",
        extra={
            "competencia": alvo,
            "uf_alvo": config.uf_alvo,
            "empresas": empresas,
            "matrizes_repetidas": repetidas,
            "situacoes": dict(situacoes),
            "arquivo": destino.name,
            "bytes_parquet": destino.stat().st_size,
        },
    )
    return Recorte(
        caminho=destino,
        uf=config.uf_alvo,
        empresas=empresas,
        matrizes_repetidas=repetidas,
        situacoes=situacoes,
    )


# --------------------------------------------------------------------- empresas

MARCA_DE_SUPRESSAO: Final = "[NUMERO SUPRIMIDO]"
"""O que fica no lugar do documento. A marca não afirma que o número era um CPF —
parte dos casos é código de SCP —, apenas que havia dígitos suficientes para ser
um e que eles não estão mais ali. Dizer "CPF" seria afirmar o que não se sabe."""

PADROES_DE_DOCUMENTO: Final = (
    r"[0-9]{3}\.[0-9]{3}\.[0-9]{3}-[0-9]{2}",
    r"[0-9]{3}\.[0-9]{3}\.[0-9]{3}\.[0-9]{2}",
    r"[0-9]{3} [0-9]{3} [0-9]{3} [0-9]{2}",
    r"[0-9]{10,}",
)
"""O que é apagado da razão social, medido contra o recorte de SP em 2026-06.

Os três primeiros são o CPF pontuado, nas variantes que a fonte realmente tem: 37
registros com hífen, 1 com ponto no lugar dele, 4 com espaço. Nenhum é alcançado
por regra de dígito corrido, porque a pontuação quebra a sequência.

O quarto é a regra de volume — 5.195.455 registros, 26,28% do recorte — e o limite
é **dez**, não onze. Onze é o comprimento do CPF, e é o que a decisão original
previa. A leitura do arquivo achou `LUIZ FIRMINO DA SILVA 6677354881`: nome de
pessoa seguido de dez dígitos, que é um CPF cujo zero à esquerda se perdeu em
algum ponto da cadeia. Descer o limite para dez custa a supressão de cerca de 110
códigos de SCP em 19,7 milhões de registros; mantê-lo em onze custa publicar um
CPF, e essa troca não é próxima.

Sequências de doze ou mais dígitos são CNPJ — cinco registros, todos públicos — e
caem na mesma regra porque separá-las só acrescenta caminho para errar. O
`cnpj_basico` já é coluna própria, então nada se perde.

Comprimento é aproximação, e sozinho ele deixa passar caso real. O que fecha a
lacuna é `PADRAO_DE_CPF_PARTIDO` e `DIGITOS_DE_CPF_ENCURTADO`, abaixo, que somam
cobertura a esta regra e nunca a substituem.
"""

PADRAO_DE_CPF_PARTIDO: Final = r"[0-9]{9}[-. ][0-9]{2}"
"""CPF canônico sem os pontos: a base de nove dígitos, um separador, os dois
dígitos verificadores.

Regra de comprimento não alcança isto, porque o separador parte a sequência em
nove e dois. São 6.530 ocorrências em Empresas, das quais **6.460 validam como
CPF** ao serem juntadas — 98,9%. Delas, **6.518 escapavam** da regra de dígito
corrido. No recorte de SP é uma só, e uma pessoa é uma pessoa.

A fonte não deixa dúvida sobre o que são: `GETULIO SOARES CRUZ CPF 177495146-00`,
`LAZARO FREITAS DE OLIVEIRA C P F 170347796 00`. A palavra vem escrita ao lado.

A supressão aqui é condicionada ao dígito verificador, não à forma: `123456789-99`
não é CPF e permanece. É a diferença entre suprimir o que é documento e suprimir
o que se parece com um."""

DIGITOS_DE_CPF_ENCURTADO: Final = 9
"""Comprimento de um CPF que perdeu dois zeros à esquerda.

Onze dígitos menos dois zeros são nove, e nove passa por baixo do limiar de dez.
São 542 sequências de nove dígitos em Empresas que validam como CPF ao serem
preenchidas à esquerda, e uma no recorte de SP: `VANDERLEI LORO 886812895`, que
é `00886812895`.

Como no padrão acima, o verificador de dígito é quem decide. Nove dígitos que não
formam CPF continuam intactos — é o que separa esta regra de baixar o limiar para
nove, que apagaria número de inscrição, código de SCP e o que mais tiver o mesmo
comprimento."""


def _encadear_supressao(patamar: str) -> str:
    """Aninha um `regexp_replace` por padrão de forma conhecida, do dentro para fora."""
    expressao = patamar
    for padrao in PADROES_DE_DOCUMENTO:
        expressao = f"regexp_replace({expressao}, '{padrao}', '{MARCA_DE_SUPRESSAO}', 'g')"
    return expressao


_MACROS: Final = f"""
CREATE OR REPLACE TEMP MACRO _soma_de_cpf(numero, tamanho) AS (
  list_sum(list_transform(generate_series(1, tamanho),
    i -> CAST(substr(numero, i, 1) AS INTEGER) * (tamanho + 2 - i)))
);

CREATE OR REPLACE TEMP MACRO cpf_valido(numero) AS (
  numero IS NOT NULL
  AND regexp_matches(numero, '^[0-9]{{11}}$')
  AND numero <> repeat(substr(numero, 1, 1), 11)
  AND (_soma_de_cpf(numero, 9) * 10 % 11) % 10 = CAST(substr(numero, 10, 1) AS INTEGER)
  AND (_soma_de_cpf(numero, 10) * 10 % 11) % 10 = CAST(substr(numero, 11, 1) AS INTEGER)
);

CREATE OR REPLACE TEMP MACRO _sem_forma_conhecida(texto) AS (
  {_encadear_supressao("texto")}
);

CREATE OR REPLACE TEMP MACRO _sem_cpf_partido(texto) AS (
  list_reduce(
    list_prepend(texto, list_filter(
      regexp_extract_all(texto, '{PADRAO_DE_CPF_PARTIDO}'),
      achado -> cpf_valido(substr(achado, 1, 9) || substr(achado, 11, 2)))),
    (acumulado, achado) -> replace(acumulado, achado, '{MARCA_DE_SUPRESSAO}'))
);

CREATE OR REPLACE TEMP MACRO _sem_cpf_encurtado(texto) AS (
  list_reduce(
    list_prepend(texto, list_filter(
      regexp_extract_all(texto, '[0-9]+'),
      achado -> length(achado) = {DIGITOS_DE_CPF_ENCURTADO}
                AND cpf_valido(lpad(achado, 11, '0')))),
    (acumulado, achado) -> replace(acumulado, achado, '{MARCA_DE_SUPRESSAO}'))
);

CREATE OR REPLACE TEMP MACRO suprimir_documentos(texto) AS (
  CASE
    WHEN texto IS NULL THEN NULL
    WHEN NOT regexp_matches(texto, '[0-9]') THEN texto
    ELSE _sem_cpf_encurtado(_sem_cpf_partido(_sem_forma_conhecida(texto)))
  END
);
"""
"""As regras de supressão, como macro do próprio motor.

Elas são compostas nesta ordem, e a ordem é o que as mantém corretas. As formas
conhecidas saem primeiro, incluindo toda sequência de dez ou mais dígitos; só
depois disso é que a busca por CPF partido pode confiar que uma sequência de nove
seguida de dois é mesmo isso, e não um pedaço de sequência maior.

Ficam no motor, e não em Python, porque um `UDF` seria chamado uma vez por linha
sobre dezenove milhões e setecentas mil delas. A validação de dígito verificador
aqui é a mesma de `cpf_valido` em `tests/test_fixtures.py`, e o teste que compara
as duas existe para que continuem sendo.
"""

DESCRICAO_DE_PORTE: Final = {
    "00": "Não informado",
    "01": "Micro empresa",
    "03": "Empresa de pequeno porte",
    "05": "Demais",
}
"""Porte não tem tabela de decodificação na fonte, então a legenda mora aqui.

Os códigos `02` e `04` não são definidos pelo PDF e não aparecem no arquivo. Eles
ficam **sem descrição** de propósito, e a quantidade vai para o log: inventar
significado é pior que devolver nulo, porque o nulo ao menos aparece.
"""

COLUNAS_SILVER_EMPRESAS: Final = (
    "cnpj_basico",
    "razao_social",
    "natureza_juridica",
    "natureza_juridica_descricao",
    "qualificacao_do_responsavel",
    "qualificacao_do_responsavel_descricao",
    "capital_social",
    "porte",
    "porte_descricao",
    "ente_federativo_responsavel",
)
"""O esquema publicável de empresas. Nenhuma coluna de contato existe em Empresas,
mas a lista é explícita para que acrescentar uma seja uma decisão visível."""

_COLUNAS_DE_CONTEUDO: Final = (
    "razao_social",
    "natureza_juridica",
    "qualificacao_do_responsavel",
    "capital_social",
    "porte",
    "ente_federativo_responsavel",
)

FORMATO_DO_CAPITAL: Final = r"^[0-9]+,[0-9]{2}$"
"""Como o capital vem: vírgula decimal, duas casas, sem separador de milhar.
Casou em 100% dos registros do recorte. É conferido em vez de suposto porque
`TRY_CAST` sobre o que não casa devolveria nulo em silêncio, que é a forma exata
de perder dado sem ninguém notar."""


def definir_macros(conexao: duckdb.DuckDBPyConnection) -> None:
    """Instala as macros de supressão na conexão. Idempotente."""
    conexao.execute(_MACROS)


def _suprimir_documentos(coluna: str) -> str:
    """Expressão SQL que apaga da coluna todo documento reconhecível."""
    return f"suprimir_documentos({coluna})"


def _campos_preenchidos() -> str:
    """Quantos campos de conteúdo a linha traz, para desempatar `cnpj_basico`."""
    return " + ".join(
        f"(CASE WHEN nullif(trim(coalesce({coluna}, '')), '') IS NULL THEN 0 ELSE 1 END)"
        for coluna in _COLUNAS_DE_CONTEUDO
    )


def _descricao_de_porte() -> str:
    ramos = " ".join(
        f"WHEN '{codigo}' THEN '{descricao}'" for codigo, descricao in DESCRICAO_DE_PORTE.items()
    )
    return (
        "CASE WHEN nullif(trim(coalesce(porte, '')), '') IS NULL THEN "
        f"'{DESCRICAO_DE_PORTE['00']}' ELSE (CASE porte {ramos} ELSE NULL END) END"
    )


@dataclass(frozen=True)
class EmpresasTipadas:
    """O que a tipagem produziu, com tudo que precisou ser decidido pelo caminho."""

    caminho: Path

    registros: int
    """Uma linha por `cnpj_basico` do recorte. Diferente disso é erro."""

    cnpj_basico_repetidos: int
    """Quantos `cnpj_basico` vinham repetidos em Empresas e colapsaram."""

    razoes_sociais_suprimidas: int
    """Quantas razões sociais perderam um documento. É o tamanho do vazamento que
    a fonte tem e o artefato publicado não terá."""

    capital_ausente: int
    natureza_sem_descricao: int
    qualificacao_sem_descricao: int
    porte_sem_descricao: int


def tipar_empresas(config: Config, competencia: str | None = None) -> EmpresasTipadas:
    """Tipa, decodifica e suprime documento da tabela de empresas do recorte.

    **O CPF sai aqui, não na API.** Os artefatos deste projeto vão para GitHub
    Release e para imagem Docker. Uma `razao_social` crua nesses lugares é um CPF
    publicado, e mascarar na resposta da API depois não desfaz a publicação. A
    supressão precisa acontecer na camada que os artefatos derivam.

    **O que sai é o documento, não o nome.** A supressão é cirúrgica: apaga o run
    de dígitos e deixa o resto da razão social intacto. `ALINE APARECIDA LEITE DE
    SOUZA 22922853802` vira `ALINE APARECIDA LEITE DE SOUZA [NUMERO SUPRIMIDO]`.

    Isto é julgamento, não consequência óbvia da regra, e as duas saídas divergem
    por completo. Apagar o campo inteiro deixaria 5,2 milhões de empresas — 26% do
    recorte — sem nome nenhum, e o grafo não serviria para elas. Não suprimir
    publicaria o CPF.

    A distinção que sustenta a escolha é que **CPF e razão social não são a mesma
    coisa**. O CPF é identificador protegido, e a própria Receita o mascara em
    `Socios`, em todo lugar onde teve a chance de mascarar. A razão social do
    empresário individual é o nome **legal do negócio**: sai em nota fiscal, em
    contrato e no cartão CNPJ. Tratar as duas igual custaria a utilidade sem
    comprar privacidade nenhuma. Ver ADR-006.

    Dois registros em 19,77 milhões ficam só com a marca, porque a razão social
    inteira era o documento — `11954952746` e `018.066.169-80`, sem nome ao lado.
    Não havia nome a preservar, e o desfecho é o certo.

    **O custo dessa recusa foi medido, e é grande.** O CPF sem máscara da razão
    social identificaria o dono de cada empresário individual, e o projeto recusa
    usá-lo — ver a decisão registrada para o ADR. O preço: das 19.770.618
    empresas do recorte de SP, **14.792.701 não têm nenhum sócio** em `Socios`,
    porque o dono do empresário individual não está lá. São 74,8% de nós isolados,
    que nenhum caminho societário jamais atravessa. A alternativa era extrair o
    CPF da razão social e reidentificar milhões de pessoas a partir de um dado que
    a Receita mascara em todo lugar onde teve a chance. Registrar o número é o que
    transforma a recusa de retórica em decisão verificável.

    **O orçamento de deploy é dos metadados, não do CSR.** A razão social de
    19,77 milhões de empresas ocupa 323 MiB já comprimida, contra um teto de 500 MB
    para o artefato inteiro; os arrays CSR são a parte pequena da conta. Excluir da
    publicação os 14.792.701 nós sem vínculo deixa cerca de 5 milhões de empresas e
    resolve o orçamento. A decisão sobre nós isolados, na Fase 4, não é otimização
    de `indptr` — é o que torna a Fase 8 possível.
    """
    alvo = competencia or config.competencia
    fonte = _fonte_de_empresas(config, alvo)
    recorte = config.data_dir / "silver" / alvo / "recorte.parquet"
    if not recorte.exists():
        raise RecorteAusenteError(
            f"Não há recorte em {recorte}. O recorte por UF define quais empresas entram, "
            "e precisa ser aplicado antes da tipagem."
        )

    destino = recorte.with_name("empresas.parquet")
    naturezas = config.data_dir / "bronze" / alvo / "naturezas.parquet"
    qualificacoes = config.data_dir / "bronze" / alvo / "qualificacoes.parquet"

    with abrir_conexao(config, config.data_dir / "duckdb-tmp") as conexao:
        definir_macros(conexao)
        # Ordem total e declarada: primeiro a linha com mais campos preenchidos,
        # depois o próprio conteúdo como critério. Duas linhas que empatem em tudo
        # são indistinguíveis, e qualquer uma delas dá o mesmo resultado — é o que
        # torna a escolha determinística sem depender da ordem de leitura.
        desempate = ", ".join(f"{coluna} NULLS LAST" for coluna in _COLUNAS_DE_CONTEUDO)
        conexao.execute(
            f"""
            CREATE OR REPLACE TEMP TABLE empresas AS
            SELECT * EXCLUDE (posicao) FROM (
              SELECT e.*,
                     row_number() OVER (
                       PARTITION BY e.cnpj_basico
                       ORDER BY {_campos_preenchidos()} DESC, {desempate}
                     ) AS posicao,
                     count(*) OVER (PARTITION BY e.cnpj_basico) AS linhas_do_cnpj
              FROM {fonte} e
              SEMI JOIN read_parquet('{recorte.as_posix()}') r USING (cnpj_basico)
            ) WHERE posicao = 1
            """
        )

        malformados = conexao.execute(
            "SELECT count(*), min(capital_social) FROM empresas "
            f"WHERE capital_social IS NOT NULL AND NOT regexp_matches(capital_social, "
            f"'{FORMATO_DO_CAPITAL}')"
        ).fetchone()
        if malformados and int(malformados[0]):
            raise CapitalSocialMalformadoError(
                f"{int(malformados[0]):,} registros têm capital_social fora de "
                f"{FORMATO_DO_CAPITAL}, o primeiro deles {malformados[1]!r}. Converter assim "
                "mesmo devolveria nulo em silêncio, e capital nulo é indistinguível de "
                "capital zero em tudo o que vier depois."
            )

        ausentes = conexao.execute(
            f"SELECT count(*) FROM read_parquet('{recorte.as_posix()}') r "
            "ANTI JOIN empresas e USING (cnpj_basico)"
        ).fetchone()
        if ausentes and int(ausentes[0]):
            raise EmpresaAusenteError(
                f"{int(ausentes[0]):,} cnpj_basico do recorte não têm linha em Empresas. O "
                "recorte define o universo do projeto, e empresa que existe como "
                "estabelecimento mas não como empresa quebra essa definição."
            )

        parcial = destino.with_name(f"{destino.name}.parcial")
        conexao.execute(
            f"COPY (SELECT cnpj_basico, "
            f"  {_suprimir_documentos('razao_social')} AS razao_social, "
            f"  natureza_juridica, "
            f"  n.descricao AS natureza_juridica_descricao, "
            f"  qualificacao_do_responsavel, "
            f"  q.descricao AS qualificacao_do_responsavel_descricao, "
            f"  CAST(replace(capital_social, ',', '.') AS DECIMAL(18,2)) AS capital_social, "
            f"  porte, "
            f"  {_descricao_de_porte()} AS porte_descricao, "
            f"  ente_federativo_responsavel "
            f"FROM empresas e "
            f"LEFT JOIN read_parquet('{naturezas.as_posix()}') n ON n.codigo = e.natureza_juridica "
            f"LEFT JOIN read_parquet('{qualificacoes.as_posix()}') q "
            f"  ON q.codigo = e.qualificacao_do_responsavel "
            f"ORDER BY cnpj_basico) "
            f"TO '{parcial.as_posix()}' (FORMAT PARQUET, COMPRESSION ZSTD)"
        )

        medidas = conexao.execute(
            f"SELECT count(*), "
            f"  count(*) FILTER (WHERE linhas_do_cnpj > 1), "
            f"  count(*) FILTER (WHERE razao_social IS NOT NULL "
            f"    AND {_suprimir_documentos('razao_social')} <> razao_social), "
            f"  count(*) FILTER (WHERE capital_social IS NULL), "
            f"  count(*) FILTER (WHERE {_descricao_de_porte()} IS NULL) "
            f"FROM empresas"
        ).fetchone()
        registros, repetidos, suprimidas, sem_capital, sem_porte = (
            tuple(int(valor) for valor in medidas) if medidas else (0, 0, 0, 0, 0)
        )

        sem_natureza = conexao.execute(
            f"SELECT count(*) FROM empresas e ANTI JOIN "
            f"read_parquet('{naturezas.as_posix()}') n ON n.codigo = e.natureza_juridica"
        ).fetchone()
        sem_descricao = int(sem_natureza[0]) if sem_natureza else 0

        sem_qualificacao = conexao.execute(
            f"SELECT count(*) FROM empresas e ANTI JOIN "
            f"read_parquet('{qualificacoes.as_posix()}') q "
            f"ON q.codigo = e.qualificacao_do_responsavel"
        ).fetchone()
        sem_qualificacao_descricao = int(sem_qualificacao[0]) if sem_qualificacao else 0

        try:
            validar_cnpj_basico_unico(conexao, parcial)
        except ErroDeSilver:
            parcial.unlink(missing_ok=True)
            raise

    parcial.replace(destino)

    logger.info(
        "empresas tipadas",
        extra={
            "competencia": alvo,
            "uf_alvo": config.uf_alvo,
            "registros": registros,
            "cnpj_basico_repetidos": repetidos,
            "razoes_sociais_suprimidas": suprimidas,
            "capital_ausente": sem_capital,
            "natureza_sem_descricao": sem_descricao,
            "qualificacao_sem_descricao": sem_qualificacao_descricao,
            "porte_sem_descricao": sem_porte,
            "arquivo": destino.name,
            "bytes_parquet": destino.stat().st_size,
        },
    )
    return EmpresasTipadas(
        caminho=destino,
        registros=registros,
        cnpj_basico_repetidos=repetidos,
        razoes_sociais_suprimidas=suprimidas,
        capital_ausente=sem_capital,
        natureza_sem_descricao=sem_descricao,
        qualificacao_sem_descricao=sem_qualificacao_descricao,
        porte_sem_descricao=sem_porte,
    )


# ----------------------------------------------------------------------- sócios

PREENCHEDOR_DE_DOCUMENTO: Final = "***000000**"
"""Máscara sem documento por trás, no campo `representante_legal`.

Ela ocupa **8.439.366 dos 8.699.764 vínculos** do recorte de SP — 97% — e em todos
eles `nome_representante` vem vazio. A correspondência é perfeita: onde há
preenchedor não há nome, e onde há documento há nome. Não é representante sem
documento; é vínculo sem representante nenhum.

O silver converte para nulo, e o motivo é a Fase 4. Um identificador só vale como
identidade se distinguir, e este não distingue nada: tratá-lo como documento
fundiria 8,4 milhões de representantes inexistentes num único nó, ligado a quase
todo o grafo. Seria o pior tipo de erro deste projeto — não uma falha, mas um
resultado plausível e falso.
"""

DESCRICAO_DE_IDENTIFICADOR_DE_SOCIO: Final = {
    "1": "Pessoa jurídica",
    "2": "Pessoa física",
    "3": "Estrangeiro",
}
"""Ver `docs/layout_rfb.md`. Os três valores foram conferidos contra o arquivo."""

DESCRICAO_DE_FAIXA_ETARIA: Final = {
    "0": "Não se aplica",
    "1": "0 a 12 anos",
    "2": "13 a 20 anos",
    "3": "21 a 30 anos",
    "4": "31 a 40 anos",
    "5": "41 a 50 anos",
    "6": "51 a 60 anos",
    "7": "61 a 70 anos",
    "8": "71 a 80 anos",
    "9": "Acima de 80 anos",
}
"""Faixa etária não tem tabela de decodificação, como porte. Os dez códigos
aparecem no recorte de SP, e nenhum outro."""

PISO_DE_ENTRADA: Final = dt.date(1900, 1, 1)
"""Abaixo disto a data não descreve entrada em sociedade nenhuma.

A fonte traz `00210823` — ano 21 — e o `try_strptime` aceita sem reclamar, porque
o ano 21 existe no calendário. É um registro em 8,7 milhões, e o piso existe para
que ele vire nulo contado em vez de sócio que entrou na sociedade sob Tibério."""

COLUNAS_SILVER_SOCIOS: Final = (
    "cnpj_basico",
    "identificador_socio",
    "identificador_socio_descricao",
    "nome_socio_ou_razao_social",
    "cnpj_cpf_socio",
    "qualificacao_socio",
    "qualificacao_socio_descricao",
    "data_entrada_sociedade",
    "pais",
    "pais_descricao",
    "representante_legal",
    "nome_representante",
    "qualificacao_representante_legal",
    "qualificacao_representante_legal_descricao",
    "faixa_etaria",
    "faixa_etaria_descricao",
)
"""O esquema publicável de sócios. Socios não tem coluna de contato na origem, e a
lista é explícita para que acrescentar uma seja decisão visível."""


@dataclass(frozen=True)
class SociosTipados:
    """O que a tipagem de sócios produziu, com o que precisou ser decidido."""

    caminho: Path

    vinculos: int
    """Uma linha por vínculo. São as arestas do grafo da Fase 4."""

    por_tipo: tuple[tuple[str, int], ...]
    """Contagem por `identificador_socio`. Dimensiona os nós antes de construí-los."""

    nomes_suprimidos: int
    representantes_sem_documento: int
    datas_invalidas: int

    qualificacao_socio_sem_descricao: int
    qualificacao_representante_sem_descricao: int
    pais_sem_descricao: int
    """Códigos **preenchidos** que a tabela de domínio não tem. Ausência é contada
    à parte, em `pais_ausente`: um campo vazio não é um código que falhou em casar,
    e somar os dois afoga o sinal — em país, 971 códigos inexistentes ficariam
    escondidos atrás de 8,65 milhões de vínculos que simplesmente não têm país."""

    pais_ausente: int


def contar(conexao: duckdb.DuckDBPyConnection, fonte: str) -> int:
    """Conta registros de uma tabela ou de uma cláusula de leitura.

    Existe como função nomeada, e não como consulta inline, para que a suíte
    consiga fazê-la mentir: guarda que nunca reprovou não provou que sabe reprovar.
    """
    resultado = conexao.execute(f"SELECT count(*) FROM {fonte}").fetchone()
    return int(resultado[0]) if resultado else -1


def _descricao_por_codigo(coluna: str, legenda: dict[str, str]) -> str:
    """CASE que decodifica enumeração sem tabela na fonte. Código novo vira nulo."""
    ramos = " ".join(f"WHEN '{codigo}' THEN '{texto}'" for codigo, texto in legenda.items())
    return f"CASE {coluna} {ramos} ELSE NULL END"


def _ultimo_dia_da_competencia(competencia: str) -> dt.date:
    """Teto da data de entrada: sócio não entra depois de a competência fechar."""
    ano, mes = (int(parte) for parte in competencia.split("-"))
    return dt.date(ano, mes, calendar.monthrange(ano, mes)[1])


FORMATO_DA_DATA: Final = r"^[0-9]{8}$"
"""Oito dígitos, `AAAAMMDD`. Casou em 100% dos 8.699.764 vínculos do recorte.

A conferência de largura **não** é redundante com a conversão. `try_strptime`
aceita `'2015031'`, sete dígitos, e devolve 2015-03-01: engole o dígito que falta
e entrega uma data plausível, sem erro e sem nulo. É a pior forma de conversão
silenciosa, porque o resultado parece certo."""


def _data_de_entrada(coluna: str, teto: dt.date) -> str:
    """Converte para DATE e anula o que não for data plausível.

    Três coisas caem no mesmo ramo e são contadas juntas: largura errada, valor que
    o `try_strptime` recusa, e data fora da faixa. Conversão silenciosa de data é o
    descarte mais fácil de não perceber, porque nada no resultado indica que havia
    um valor ali — por isso o nulo é contado, e não apenas produzido.
    """
    convertida = f"try_strptime({coluna}, '%Y%m%d')::DATE"
    return (
        f"CASE WHEN regexp_matches({coluna}, '{FORMATO_DA_DATA}') "
        f"AND {convertida} BETWEEN DATE '{PISO_DE_ENTRADA}' AND DATE '{teto}' "
        f"THEN {convertida} ELSE NULL END"
    )


def tipar_socios(config: Config, competencia: str | None = None) -> SociosTipados:
    """Tipa, decodifica e suprime documento da tabela de sócios do recorte.

    Esta é a tabela com mais superfície de dado pessoal do projeto, e é de onde
    saem as arestas do grafo. As duas coisas ao mesmo tempo é o que a torna
    delicada: cada linha é um vínculo societário e uma pessoa.

    **O que a medição encontrou, e que o PDF não garantia.** O documento afirma
    que sócio e representante vêm descaracterizados, e desta vez ele acerta: os
    8.424.780 CPF de pessoa física e os 8.699.764 de representante estão **todos**
    mascarados, sem uma exceção, e o estrangeiro não traz documento nenhum. A
    conferência foi feita porque o mesmo PDF já errou três vezes sobre este
    arquivo, não porque houvesse suspeita.

    **O detector de CPF foi apontado para os campos de nome, e não deu zero.**
    `nome_socio_ou_razao_social` traz três CPF válidos — em registros tipados como
    pessoa jurídica, onde ninguém iria procurar. Foi assim que `razao_social` se
    revelou, e a lição é a mesma: campo chamado "nome" não contém só nome. A regra
    do commit 16 se aplica aqui inteira. `nome_representante` deu zero, mas zero
    **medido**, que é o que permite afirmá-lo.

    **Nenhum vínculo se perde.** A contagem é conferida antes e depois. Divergir
    significa que um join descartou aresta, e aresta descartada em silêncio é
    caminho societário que deixa de existir sem ninguém saber que existia — o modo
    de falha que a Fase 5 não teria como detectar.
    """
    alvo = competencia or config.competencia
    fonte = _fonte_do_bronze(config, alvo, "socios")
    recorte = config.data_dir / "silver" / alvo / "recorte.parquet"
    if not recorte.exists():
        raise RecorteAusenteError(
            f"Não há recorte em {recorte}. O recorte por UF define quais vínculos entram, "
            "e precisa ser aplicado antes da tipagem."
        )

    destino = recorte.with_name("socios.parquet")
    qualificacoes = config.data_dir / "bronze" / alvo / "qualificacoes.parquet"
    paises = config.data_dir / "bronze" / alvo / "paises.parquet"
    teto = _ultimo_dia_da_competencia(alvo)
    tipo_de_socio = _descricao_por_codigo(
        "s.identificador_socio", DESCRICAO_DE_IDENTIFICADOR_DE_SOCIO
    )
    faixa = _descricao_por_codigo("s.faixa_etaria", DESCRICAO_DE_FAIXA_ETARIA)

    with abrir_conexao(config, config.data_dir / "duckdb-tmp") as conexao:
        definir_macros(conexao)
        conexao.execute(
            f"CREATE OR REPLACE TEMP TABLE socios AS SELECT s.* FROM {fonte} s "
            f"SEMI JOIN read_parquet('{recorte.as_posix()}') r USING (cnpj_basico)"
        )
        vinculos = contar(conexao, "socios")

        parcial = destino.with_name(f"{destino.name}.parcial")
        conexao.execute(
            f"""
            COPY (
              SELECT
                s.cnpj_basico,
                s.identificador_socio,
                {tipo_de_socio} AS identificador_socio_descricao,
                suprimir_documentos(s.nome_socio_ou_razao_social) AS nome_socio_ou_razao_social,
                s.cnpj_cpf_socio,
                s.qualificacao_socio,
                qs.descricao AS qualificacao_socio_descricao,
                {_data_de_entrada("s.data_entrada_sociedade", teto)} AS data_entrada_sociedade,
                s.pais,
                p.descricao AS pais_descricao,
                nullif(s.representante_legal, '{PREENCHEDOR_DE_DOCUMENTO}') AS representante_legal,
                suprimir_documentos(nullif(trim(coalesce(s.nome_representante, '')), ''))
                  AS nome_representante,
                s.qualificacao_representante_legal,
                qr.descricao AS qualificacao_representante_legal_descricao,
                s.faixa_etaria,
                {faixa} AS faixa_etaria_descricao
              FROM socios s
              LEFT JOIN read_parquet('{qualificacoes.as_posix()}') qs
                ON qs.codigo = s.qualificacao_socio
              LEFT JOIN read_parquet('{qualificacoes.as_posix()}') qr
                ON qr.codigo = s.qualificacao_representante_legal
              LEFT JOIN read_parquet('{paises.as_posix()}') p ON p.codigo = s.pais
              ORDER BY ALL
            ) TO '{parcial.as_posix()}' (FORMAT PARQUET, COMPRESSION ZSTD)
            """
        )

        gravados = contar(conexao, f"read_parquet('{parcial.as_posix()}')")
        if gravados != vinculos:
            parcial.unlink(missing_ok=True)
            raise VinculoPerdidoError(
                f"Entraram {vinculos:,} vínculos e saíram {gravados:,}. Algum join descartou "
                "aresta, e aresta descartada em silêncio é caminho societário que deixa de "
                "existir sem ninguém saber que existia. Confira as decodificações: toda "
                "junção com tabela de domínio precisa ser LEFT."
            )

        medidas = conexao.execute(
            f"""
            SELECT
              count(*) FILTER (
                WHERE nome_socio_ou_razao_social IS NOT NULL
                  AND suprimir_documentos(nome_socio_ou_razao_social)
                      <> nome_socio_ou_razao_social),
              count(*) FILTER (WHERE representante_legal = '{PREENCHEDOR_DE_DOCUMENTO}'),
              count(*) FILTER (
                WHERE {_data_de_entrada("data_entrada_sociedade", teto)} IS NULL)
            FROM socios
            """
        ).fetchone()
        suprimidos, sem_representante, datas_invalidas = (
            tuple(int(valor) for valor in medidas) if medidas else (0, 0, 0)
        )

        por_tipo = tuple(
            (str(codigo), int(quantos))
            for codigo, quantos in conexao.execute(
                "SELECT identificador_socio, count(*) FROM socios GROUP BY 1 ORDER BY 1"
            ).fetchall()
        )

        def sem_correspondencia(coluna: str, tabela: Path) -> int:
            """Códigos preenchidos que a tabela de domínio não tem.

            O `IS NOT NULL` não é detalhe: sem ele, um campo vazio entra na conta
            como se fosse código órfão, e o número deixa de medir o que promete.
            """
            resultado = conexao.execute(
                f"SELECT count(*) FROM socios s ANTI JOIN read_parquet('{tabela.as_posix()}') d "
                f"ON d.codigo = s.{coluna} WHERE s.{coluna} IS NOT NULL"
            ).fetchone()
            return int(resultado[0]) if resultado else 0

        sem_qualificacao = sem_correspondencia("qualificacao_socio", qualificacoes)
        sem_representante_descricao = sem_correspondencia(
            "qualificacao_representante_legal", qualificacoes
        )
        sem_pais = sem_correspondencia("pais", paises)
        pais_ausente = contar(conexao, "socios WHERE pais IS NULL")

    parcial.replace(destino)

    logger.info(
        "sócios tipados",
        extra={
            "competencia": alvo,
            "uf_alvo": config.uf_alvo,
            "vinculos": vinculos,
            "por_tipo": dict(por_tipo),
            "nomes_suprimidos": suprimidos,
            "representantes_sem_documento": sem_representante,
            "datas_invalidas": datas_invalidas,
            "qualificacao_socio_sem_descricao": sem_qualificacao,
            "qualificacao_representante_sem_descricao": sem_representante_descricao,
            "pais_sem_descricao": sem_pais,
            "pais_ausente": pais_ausente,
            "arquivo": destino.name,
            "bytes_parquet": destino.stat().st_size,
        },
    )
    return SociosTipados(
        caminho=destino,
        vinculos=vinculos,
        por_tipo=por_tipo,
        nomes_suprimidos=suprimidos,
        representantes_sem_documento=sem_representante,
        datas_invalidas=datas_invalidas,
        qualificacao_socio_sem_descricao=sem_qualificacao,
        qualificacao_representante_sem_descricao=sem_representante_descricao,
        pais_sem_descricao=sem_pais,
        pais_ausente=pais_ausente,
    )
