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
para uma linha, o desempate é pelo menor `cnpj_ordem` — determinístico, para o
artefato ser o mesmo entre execuções — e a quantidade de casos vai para o log de
toda execução, inclusive quando é zero. Colapso silencioso é o que não pode
acontecer; colapso contado é aritmética honesta.
"""

from __future__ import annotations

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


class ErroDeSilver(RuntimeError):
    """Falha ao construir a camada silver."""


class BronzeAusenteError(ErroDeSilver):
    """A competência pedida não tem camada bronze em disco."""


class RecorteVazioError(ErroDeSilver):
    """Nenhuma matriz na UF alvo."""


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
    """Cláusula de leitura das partições de Estabelecimentos do bronze.

    A ausência é conferida aqui para que ela chegue como instrução do que fazer.
    Sem isto o DuckDB levanta uma `IOException` sobre um glob que não casou, e
    quem só esqueceu de rodar o bronze precisa deduzir isso de um erro de I/O.
    """
    entrada = config.data_dir / "bronze" / competencia
    padrao = entrada / "estabelecimentos*.parquet"
    if not sorted(entrada.glob(padrao.name)):
        raise BronzeAusenteError(
            f"Nenhum Parquet de estabelecimentos em {entrada}. Rode o bronze desta "
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
            f"       arg_min(situacao_cadastral, cnpj_ordem) AS situacao_cadastral, "
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
