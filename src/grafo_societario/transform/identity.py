"""Normalização de nome de pessoa física, e o limite dela.

A identidade de pessoa física neste projeto é o par nome mais CPF mascarado. A
máscara deixa seis dígitos visíveis, e no recorte de SP são **584.902 máscaras
distintas para 5.635.007 identidades** — 9,6 pessoas por máscara, e **49,4% das
máscaras já são compartilhadas por mais de um nome**. O nome faz quase todo o
trabalho de distinguir uma pessoa da outra.

Isso é o que torna cada degrau de normalização caro: ele apaga parte do que
distingue, e transfere o trabalho para a coincidência. Normalizar demais funde
pessoas diferentes; normalizar de menos separa a mesma pessoa. Nenhum dos dois
erros aparece como erro — os dois aparecem como grafo.

## Os três tipos de degrau, e por que a normalização para onde para

Esta é a regra que decide o que entra aqui, e existe para que a pergunta "posso
acrescentar mais um?" tenha resposta antes de alguém tentar.

**Tipo 1 — variação de codificação.** Maiúscula, acento, espaço. Não carregam
informação de identidade nenhuma: `JOSÉ` e `JOSE` são o mesmo nome escrito por
teclados diferentes. São seguros **por construção**, e não por medição — nenhum
dado poderia torná-los perigosos. Entram.

**Tipo 2 — ruído gramatical.** Partícula: `DA`, `DAS`, `DE`, `DI`, `DO`, `DOS`,
`E`. Não distinguem pessoas; distinguem estilos de preenchimento de formulário.
Não são seguros por construção, então foram **medidos**: sobre 5.635.007
identidades, a remoção funde **sete**, e as sete são a mesma pessoa digitada de
dois jeitos, com a mesma máscara de CPF: pares que diferem apenas por um `DA`, um
`DE` ou um `DOS` a mais. Sem o degrau, essas sete pessoas ficariam como catorze
nós. Entra.

Os nomes reais dos sete não aparecem aqui nem nos testes. São pessoas físicas
identificáveis, e este é um repositório público — os testes reproduzem os mesmos
padrões com nomes sintéticos.

**Tipo 3 — informação.** Inicial do meio, sobrenome intermediário, apelido. A
inicial existe **para** distinguir: é o que separa `JOSE C SILVA` de `JOSE A
SILVA`. Remover mede zero fusão hoje, mas o mecanismo funde **por construção**
assim que dois portadores da mesma máscara diferirem só pela inicial — e com
metade das máscaras já compartilhada, isso vai acontecer. Seguro que pode causar
o sinistro não é seguro. **Não entra, e este parágrafo é a razão.**

Se você veio aqui para acrescentar um degrau, o teste é esse: ele apaga
codificação, gramática, ou informação? Só os dois primeiros passam. "Remover
sobrenome do meio" é tipo 3, mesmo parecendo tão inofensivo quanto remover `DA`.

## Nome ausente não é nome vazio

`normalizar_nome` devolve `None` — nunca `""` — quando não sobra nome. São 370
vínculos de pessoa física no recorte de SP sem nome nenhum, e para eles a
identidade seria a máscara sozinha, que funde todos os que a compartilham. Esse
é exatamente o pior caso que os números acima descrevem.

Devolver `None` obriga quem gera o identificador a decidir o que fazer com o
caso, em vez de hashear string vazia e fundir por acidente. A decisão em si é da
geração de identidade, não daqui.
"""

from __future__ import annotations

import hashlib
import logging
import unicodedata
from dataclasses import dataclass
from pathlib import Path
from typing import Final

import duckdb

from grafo_societario.config import Config
from grafo_societario.transform.bronze import abrir_conexao

logger = logging.getLogger(__name__)

PARTICULAS: Final = frozenset({"DA", "DAS", "DE", "DI", "DO", "DOS", "E"})
"""Partículas removidas como **token inteiro**, nunca como prefixo.

A distinção não é detalhe: `DANIEL` começa com `DA` e `EDUARDO` começa com `E`.
Remover por prefixo transformaria os dois em outra pessoa, e o erro passaria por
normalização bem-sucedida.
"""


def normalizar_nome(nome: str | None) -> str | None:
    """Reduz um nome à forma que a identidade compara.

    Aplica, nesta ordem: remoção de acento, maiúscula, colapso de espaço e
    remoção de partícula. Devolve `None` quando não sobra nome — entrada nula,
    vazia, só espaço, ou composta apenas de partículas.
    """
    if nome is None:
        return None

    sem_acento = unicodedata.normalize("NFKD", nome).encode("ascii", "ignore").decode("ascii")
    tokens = [token for token in sem_acento.upper().split() if token not in PARTICULAS]
    return " ".join(tokens) or None


_LISTA_DE_PARTICULAS: Final = ", ".join(f"'{particula}'" for particula in sorted(PARTICULAS))

MACRO_DE_NORMALIZACAO: Final = f"""
CREATE OR REPLACE TEMP MACRO normalizar_nome(nome) AS (
  nullif(
    array_to_string(
      list_filter(
        string_split_regex(upper(strip_accents(coalesce(nome, ''))), '\\s+'),
        token -> token <> '' AND token NOT IN ({_LISTA_DE_PARTICULAS})
      ), ' '),
    '')
);
"""
"""A mesma regra no motor, para as 8,4 milhões de linhas da camada silver.

Existe em SQL porque um `UDF` em Python seria chamado uma vez por vínculo. Existe
também em Python porque a regra precisa ser legível e testável fora do motor. Duas
implementações do mesmo algoritmo divergem no commit em que ninguém olha, e é por
isso que há um teste comparando as duas — ele é o que as mantém sendo uma só.
"""


def instalar_normalizacao(conexao: duckdb.DuckDBPyConnection) -> None:
    """Registra `normalizar_nome` na conexão do DuckDB. Idempotente."""
    conexao.execute(MACRO_DE_NORMALIZACAO)


# --------------------------------------------------------------- identificador

SEPARADOR: Final = "|"
"""Separa as partes antes do hash, para que `AB` mais `C` não colida com `A` mais
`BC`. Conferido contra os 8.699.764 nomes do recorte: nenhum o contém."""

DIGITOS_DO_IDENTIFICADOR: Final = 16
"""Metade de um SHA-256, 64 bits.

Sobre 5,6 milhões de nós, a chance de dois identificadores diferentes colidirem
aqui é da ordem de 1 em 10⁶ — **cem mil vezes menor** que a colisão semântica de
1 em 92.186 que o próprio dado impõe. O hash não é o gargalo da identidade, e
alargá-lo não compraria precisão nenhuma; o que limita é o CPF mascarado.
"""

TIPOS: Final = {
    "1": "pessoa_juridica",
    "2": "pessoa_fisica",
    "3": "estrangeiro",
}

EXATA: Final = "exata"
"""Pessoa jurídica: o `cnpj_basico` identifica sem ambiguidade. Não há estimativa
a fazer, e a taxa de colisão fica nula em vez de zero — nulo é "não se aplica"."""

ESTIMADA: Final = "estimada"
"""Pessoa física com nome: nome normalizado mais CPF mascarado. É a única classe
com taxa de colisão calculável, e ela vem medida, não arbitrada."""

FRACA: Final = "fraca"
"""Estrangeiro: sem documento nenhum, sobra nome e país. Ver ADR-004. A fusão é
materialmente mais frágil que a de pessoa física, e não há máscara que a
sustente — por isso é sinalizada em vez de estimada."""

NAO_FUNDIVEL: Final = "nao_fundivel"
"""Sócio sem nome: 370 vínculos de pessoa física no recorte de SP.

A identidade seria a máscara sozinha, que funde todos os 48,8 portadores médios
de uma máscara da região 8. Cada registro vira um nó próprio: isolar 370 custa
nada, fundi-los é uma afirmação falsa sobre quem é sócio de quem.
"""


def identificador(*partes: str) -> str:
    """Chave estável de um nó, a partir das partes que o definem.

    Estável entre execuções, entre máquinas e entre competências: é função apenas
    do conteúdo. Duas execuções sobre o mesmo dado produzem o mesmo grafo, que é
    o que a Fase 8 promete e a Fase 4 precisa para indexar.
    """
    juntas = SEPARADOR.join(partes).encode("utf-8")
    return hashlib.sha256(juntas).hexdigest()[:DIGITOS_DO_IDENTIFICADOR]


MACRO_DE_IDENTIFICADOR: Final = f"""
CREATE OR REPLACE TEMP MACRO identificador(partes) AS (
  substr(sha256(array_to_string(partes, '{SEPARADOR}')), 1, {DIGITOS_DO_IDENTIFICADOR})
);
"""


def instalar_identificador(conexao: duckdb.DuckDBPyConnection) -> None:
    """Registra `normalizar_nome` e `identificador` na conexão. Idempotente."""
    instalar_normalizacao(conexao)
    conexao.execute(MACRO_DE_IDENTIFICADOR)


# ------------------------------------------------------------------- artefato

COLUNAS_IDENTIDADES: Final = (
    "identificador",
    "tipo",
    "nome",
    "cnpj_basico",
    "cpf_mascarado",
    "pais",
    "no_recorte",
    "confianca",
    "taxa_de_colisao",
    "vinculos_no_recorte",
)
"""O esquema das identidades.

`vinculos_no_recorte` chama-se assim, e não `grau`, de propósito. **Todo grau
neste grafo é relativo ao recorte.** Só ingerimos sócios de empresas cuja matriz
está na UF alvo, então quem é sócio de 3 empresas em SP e 40 no Rio aparece aqui
com 3. O número é piso, nunca total, e o nome da coluna é a única coisa que
impede isso de se perder entre esta camada e um gráfico de LinkedIn.

Vale para a Fase 5, onde grau e centralidade são "dentro do recorte" e precisam
ser nomeados assim, e vale para a Fase 9, onde "pessoa com participação em N
empresas" é afirmação falsa se N for lido como total.
"""


class ErroDeIdentidade(RuntimeError):
    """Falha ao gerar as identidades."""


class SociosAusentesError(ErroDeIdentidade):
    """A geração foi pedida antes de a tabela de sócios existir."""


@dataclass(frozen=True)
class Identidades:
    """O que a geração produziu, com a incerteza medida junto."""

    caminho: Path

    total: int
    por_tipo: tuple[tuple[str, int], ...]
    por_confianca: tuple[tuple[str, int], ...]

    externos: int
    """Pessoas jurídicas sócias cuja empresa está fora do recorte. Elas entram
    como **conector**, não como nó completo — ver `gerar_identidades`."""

    fusoes_estimadas: float
    """Quantas identidades de pessoa física devem ser, na verdade, duas pessoas."""

    taxa_por_regiao: tuple[tuple[str, float], ...]
    """Taxa de colisão por dígito de região fiscal. A média esconderia que o risco
    é vinte vezes maior na região 8, e publicar a média tendo a distribuição é
    desperdiçar informação já medida."""


_TIPO = (
    "CASE identificador_socio "
    + " ".join(f"WHEN '{codigo}' THEN '{nome}'" for codigo, nome in TIPOS.items())
    + " ELSE NULL END"
)

_CONFIANCA = f"""
CASE
  WHEN identificador_socio = '1' THEN '{EXATA}'
  WHEN identificador_socio = '2' AND nome IS NOT NULL AND cnpj_cpf_socio IS NOT NULL
    THEN '{ESTIMADA}'
  WHEN identificador_socio = '3' AND nome IS NOT NULL THEN '{FRACA}'
  ELSE '{NAO_FUNDIVEL}'
END
"""

_IDENTIFICADOR = f"""
CASE confianca
  WHEN '{EXATA}' THEN identificador(['{TIPOS["1"]}', substr(cnpj_cpf_socio, 1, 8)])
  WHEN '{ESTIMADA}' THEN identificador(['{TIPOS["2"]}', nome, cnpj_cpf_socio])
  WHEN '{FRACA}' THEN identificador(['{TIPOS["3"]}', nome, coalesce(pais, '')])
  ELSE identificador(['{NAO_FUNDIVEL}', cnpj_basico, coalesce(cnpj_cpf_socio, ''),
                      coalesce(nome, ''), coalesce(pais, '')])
END
"""
"""A chave inclui o tipo, para que um `cnpj_basico` e um nome nunca colidam por
acaso. A de `nao_fundivel` inclui a empresa: sem nome não há o que fundir, então
cada vínculo vira um nó, e a empresa é o que os mantém distintos entre si."""

_TAXA_EM_INTEIROS: Final = """
A taxa de colisão é somada em inteiros, e não em ponto flutuante.

Adição de `double` não é associativa, e o motor agrega em paralelo com ordem de
partição que varia entre execuções. Somar `pow(q/n, 2)` diretamente produz um
resultado que difere nos últimos bits a cada rodada — e um artefato que muda sem o
dado ter mudado quebra a imutabilidade que a Fase 8 promete. Medido: o Parquet
saía com bytes diferentes em duas execuções seguidas sobre o mesmo silver, com
tudo o mais idêntico.

A correção é estrutural, não um arredondamento que quase sempre funciona: soma-se
`q * q` e `q * (q - 1) / 2`, que são inteiros exatos e cuja adição **é**
associativa, e divide-se uma vez no fim. `q * (q - 1)` é sempre par, então a
divisão inteira também é exata. Ordem de agregação deixa de importar por
construção.
"""


def consulta_de_socios_identificados(socios: Path) -> str:
    """SELECT que anexa tipo, confiança e identificador a cada vínculo.

    Público porque tem dois consumidores. A geração usa para produzir o artefato;
    a verificação de qualidade usa para recomputar e conferir que o artefato em
    disco corresponde ao silver em disco. Duplicar a expressão nos dois lugares
    faria a conferência passar a comparar duas regras em vez de uma.

    A Fase 4 usa a mesma consulta para ligar vínculo a nó: o identificador é a
    chave de junção, porque `identidades.nome` guarda a grafia de exibição e não
    a normalizada.
    """
    return f"""
    SELECT cnpj_basico,
           identificador_socio,
           normalizar_nome(nome_socio_ou_razao_social) AS nome,
           nome_socio_ou_razao_social AS nome_de_origem,
           cnpj_cpf_socio,
           pais,
           {_TIPO} AS tipo,
           {_CONFIANCA} AS confianca
    FROM read_parquet('{socios.as_posix()}')
    """


def gerar_identidades(config: Config, competencia: str | None = None) -> Identidades:
    """Gera o identificador estável de cada sócio do recorte.

    **Quatro caminhos de identidade, e só um deles é exato.**

    Pessoa jurídica tem `cnpj_basico`: identifica sem ambiguidade, e não há
    estimativa a fazer. Pessoa física tem nome mais CPF mascarado, e é a única
    classe onde a colisão é calculável. Estrangeiro não tem documento nenhum —
    sobra nome e país, e a fusão é materialmente mais frágil (ADR-004). Sócio sem
    nome não tem identidade fundível, e cada registro vira um nó próprio.

    **A colisão é medida, não estimada de boca.** A máscara deixa seis dígitos
    visíveis, mas o último deles é a região fiscal, e num recorte de SP ele é `8`
    em 86,65% dos casos. O espaço efetivo é **132.705**, não 10⁶ — sete vezes e
    meia menor que o nominal. Ignorar isso torna qualquer estimativa otimista na
    mesma proporção.

    A taxa sai do índice de Simpson da distribuição empírica de máscaras, vezes o
    número de pares homônimos, por dígito de região. No recorte de SP: **1
    identidade em 92.186 é duas pessoas na região 8, e 1 em 1.984.377 fora dela**
    — vinte vezes de diferença, que uma média única apagaria. O modelo foi
    calibrado prevendo quantas máscaras distintas deveriam existir: 586.037
    contra 584.902 observadas, 0,19% de erro.

    **Sócio pessoa jurídica de fora do recorte entra como conector.** São 50.942
    vínculos, 19% dos vínculos entre empresas, apontando para 36.810 empresas de
    outras UFs. Descartá-los quebraria caminho real: duas paulistas ligadas por
    uma holding carioca deixariam de ter vínculo, e responder se o vínculo existe
    é o produto. O recorte define de quem partimos, não por onde o caminho passa.

    Esses nós são **conector, não nó completo**. Só ingerimos sócios de empresas
    do recorte, então a holding carioca aparece ligada às suas controladas
    paulistas e invisível em todo o resto. `vinculos_no_recorte` dela não é o grau
    dela — e isso vale para todo nó deste grafo, inclusive pessoa física.
    """
    alvo = competencia or config.competencia
    socios = config.data_dir / "silver" / alvo / "socios.parquet"
    if not socios.exists():
        raise SociosAusentesError(
            f"Não há sócios tipados em {socios}. A identidade parte da camada silver, "
            "onde o documento já foi suprimido do nome."
        )
    recorte = socios.with_name("recorte.parquet")
    destino = socios.with_name("identidades.parquet")

    with abrir_conexao(config, config.data_dir / "duckdb-tmp") as conexao:
        instalar_identificador(conexao)
        conexao.execute(
            f"CREATE OR REPLACE TEMP TABLE socio AS {consulta_de_socios_identificados(socios)}"
        )
        # `no_recorte` é marcado aqui, uma vez, e não por junção dentro da
        # agregação: um LEFT JOIN das 8,7 milhões de linhas contra os 19,7 milhões
        # do recorte, com predicado de confiança no ON, não fecha em tempo útil.
        conexao.execute(
            f"""
            CREATE OR REPLACE TEMP TABLE identificado AS
            SELECT *,
                   {_IDENTIFICADOR} AS identificador,
                   CASE WHEN confianca = '{EXATA}' THEN substr(cnpj_cpf_socio, 1, 8) IN (
                          SELECT cnpj_basico FROM read_parquet('{recorte.as_posix()}')) END
                     AS no_recorte
            FROM socio
            """
        )

        # A taxa por região sai da distribuição real das identidades de pessoa
        # física, e não de um número herdado da competência anterior.
        conexao.execute(
            f"""
            CREATE OR REPLACE TEMP TABLE taxa AS
            WITH pessoa AS (
              SELECT DISTINCT substr(cnpj_cpf_socio, 9, 1) AS regiao, nome, cnpj_cpf_socio
              FROM identificado WHERE confianca = '{ESTIMADA}'
            ),
            tamanho AS (SELECT regiao, count(*) AS identidades FROM pessoa GROUP BY 1),
            -- Somas INTEIRAS, e uma única divisão no fim. Ver `_TAXA_EM_INTEIROS`.
            quadrados AS (
              SELECT regiao, sum(quantas * quantas) AS soma_de_quadrados
              FROM (SELECT regiao, cnpj_cpf_socio, count(*) AS quantas
                    FROM pessoa GROUP BY 1, 2)
              GROUP BY 1
            ),
            homonimos AS (
              SELECT regiao, sum(quantas * (quantas - 1) / 2) AS pares
              FROM (SELECT regiao, nome, count(*) AS quantas FROM pessoa GROUP BY 1, 2)
              GROUP BY 1
            )
            SELECT t.regiao,
                   t.identidades,
                   CAST(q.soma_de_quadrados AS DOUBLE)
                     / (CAST(t.identidades AS DOUBLE) * t.identidades) AS colisao,
                   h.pares,
                   CAST(h.pares AS DOUBLE) * CAST(q.soma_de_quadrados AS DOUBLE)
                     / (CAST(t.identidades AS DOUBLE) * t.identidades) AS fusoes,
                   CAST(h.pares AS DOUBLE) * CAST(q.soma_de_quadrados AS DOUBLE)
                     / (CAST(t.identidades AS DOUBLE) * t.identidades * t.identidades)
                     AS taxa_de_colisao
            FROM tamanho t JOIN quadrados q USING (regiao) JOIN homonimos h USING (regiao)
            """
        )

        parcial = destino.with_name(f"{destino.name}.parcial")
        conexao.execute(
            f"""
            COPY (
              SELECT
                i.identificador,
                any_value(i.tipo) AS tipo,
                min(i.nome_de_origem) AS nome,
                CASE WHEN any_value(i.tipo) = '{TIPOS["1"]}'
                     THEN min(substr(i.cnpj_cpf_socio, 1, 8)) END AS cnpj_basico,
                CASE WHEN any_value(i.tipo) = '{TIPOS["2"]}'
                     THEN min(i.cnpj_cpf_socio) END AS cpf_mascarado,
                CASE WHEN any_value(i.tipo) = '{TIPOS["3"]}' THEN min(i.pais) END AS pais,
                bool_or(i.no_recorte) AS no_recorte,
                any_value(i.confianca) AS confianca,
                any_value(t.taxa_de_colisao) AS taxa_de_colisao,
                count(*) AS vinculos_no_recorte
              FROM identificado i
              LEFT JOIN taxa t
                ON t.regiao = substr(i.cnpj_cpf_socio, 9, 1) AND i.confianca = '{ESTIMADA}'
              GROUP BY i.identificador
              ORDER BY i.identificador
            ) TO '{parcial.as_posix()}' (FORMAT PARQUET, COMPRESSION ZSTD)
            """
        )

        resumo = conexao.execute(
            f"SELECT count(*), count(*) FILTER (WHERE no_recorte IS FALSE) "
            f"FROM read_parquet('{parcial.as_posix()}')"
        ).fetchone()
        total, externos = (int(resumo[0]), int(resumo[1])) if resumo else (0, 0)

        por_tipo = tuple(
            (str(nome), int(quantos))
            for nome, quantos in conexao.execute(
                f"SELECT tipo, count(*) FROM read_parquet('{parcial.as_posix()}') "
                "GROUP BY 1 ORDER BY 1"
            ).fetchall()
        )
        por_confianca = tuple(
            (str(nome), int(quantos))
            for nome, quantos in conexao.execute(
                f"SELECT confianca, count(*) FROM read_parquet('{parcial.as_posix()}') "
                "GROUP BY 1 ORDER BY 1"
            ).fetchall()
        )
        regioes = conexao.execute(
            "SELECT regiao, taxa_de_colisao, fusoes FROM taxa ORDER BY regiao"
        ).fetchall()
        taxa_por_regiao = tuple((str(regiao), float(taxa)) for regiao, taxa, _ in regioes)
        fusoes = sum(float(fusao) for _, _, fusao in regioes)

    parcial.replace(destino)

    logger.info(
        "identidades geradas",
        extra={
            "competencia": alvo,
            "uf_alvo": config.uf_alvo,
            "identidades": total,
            "por_tipo": dict(por_tipo),
            "por_confianca": dict(por_confianca),
            "externos": externos,
            "fusoes_estimadas": round(fusoes, 1),
            "taxa_por_regiao": {regiao: taxa for regiao, taxa in taxa_por_regiao},
            "arquivo": destino.name,
            "bytes_parquet": destino.stat().st_size,
        },
    )
    return Identidades(
        caminho=destino,
        total=total,
        por_tipo=por_tipo,
        por_confianca=por_confianca,
        externos=externos,
        fusoes_estimadas=fusoes,
        taxa_por_regiao=taxa_por_regiao,
    )
