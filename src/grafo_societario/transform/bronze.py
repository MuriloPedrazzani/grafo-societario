"""Camada bronze: CSV da Receita vira Parquet, sem alteração de conteúdo.

Quatro disciplinas sustentam este módulo.

**Nenhuma coluna é inferida.** O nome e o tipo de cada uma das trinta colunas de
Estabelecimentos são declarados explicitamente, e todos são `VARCHAR`. Inferência
sobre dado público sujo é onde se perde registro em silêncio: o mesmo campo traz
`01` e `2` para situação cadastral, e um tipo inteiro faria os dois colidirem.

**A contagem de registros é assertiva, não relatório.** O CSV é contado antes, o
Parquet é contado depois, e divergência interrompe a conversão. `Estabelecimentos6`
tem 4.753.436 linhas físicas para 4.753.435 registros: se o número do Parquet for o
primeiro, o parser tratou quebra de linha dentro de campo citado como fim de
registro, e é melhor descobrir aqui do que na construção do grafo.

**A entrada é UTF-8, e isso não é detalhe reversível.** O leitor de CSV do DuckDB
recusa a faixa de controle C1 ao ler como latin-1, e é por isso que a extração
transcodifica. Ver ADR-008.

**O limite de memória é declarado, não herdado.** O projeto promete rodar em 8 GiB,
e o maior arquivo tem 6,8 GB. Sem limite explícito o DuckDB se serve do que houver
e a promessa vale só na máquina de quem a escreveu.

**Nada daqui é publicado.** O bronze é fiel à origem, e a origem contém dado
pessoal: `razao_social` em Empresas traz CPF sem máscara nos registros de
empresário individual, e Socios traz nome de pessoa física. Estes Parquet moram em
`data/`, que é local e ignorado pelo git. Toda exposição parte do silver, onde a
pseudonimização já aconteceu. Ver ADR-006.
"""

from __future__ import annotations

import logging
import shutil
from dataclasses import dataclass
from pathlib import Path
from typing import Final

import duckdb

from grafo_societario.config import Config

logger = logging.getLogger(__name__)

CODIFICACAO_DA_ENTRADA: Final = "utf-8"

COLUNAS_ESTABELECIMENTOS: Final = (
    "cnpj_basico",
    "cnpj_ordem",
    "cnpj_dv",
    "identificador_matriz_filial",
    "nome_fantasia",
    "situacao_cadastral",
    "data_situacao_cadastral",
    "motivo_situacao_cadastral",
    "nome_cidade_exterior",
    "pais",
    "data_inicio_atividade",
    "cnae_fiscal_principal",
    "cnae_fiscal_secundaria",
    "tipo_logradouro",
    "logradouro",
    "numero",
    "complemento",
    "bairro",
    "cep",
    "uf",
    "municipio",
    "ddd_1",
    "telefone_1",
    "ddd_2",
    "telefone_2",
    "ddd_fax",
    "fax",
    "correio_eletronico",
    "situacao_especial",
    "data_situacao_especial",
)
"""As trinta colunas do layout, na ordem da fonte. Ver docs/layout_rfb.md."""

COLUNAS_EMPRESAS: Final = (
    "cnpj_basico",
    # ATENÇÃO: em 78% dos registros a natureza jurídica é 2135, empresário
    # individual, e nesses a razão social é o nome completo da pessoa seguido do
    # CPF SEM MÁSCARA. O bronze mantém, porque é fiel à origem e vive em data/,
    # que é local e ignorado pelo git. Este Parquet NUNCA é publicado — nem em
    # Release, nem em imagem, nem em resposta de API. A redação acontece no
    # silver. Se você veio aqui pensando em publicar o bronze por ser "só dado
    # público", é esta coluna que torna isso um vazamento de CPF.
    "razao_social",
    "natureza_juridica",
    "qualificacao_do_responsavel",
    # Vem com vírgula decimal, como "5000,00", em 100% dos 600 mil valores
    # medidos. Fica VARCHAR: converter para decimal é trabalho do silver, e a
    # fidelidade do bronze vale para tipo também.
    "capital_social",
    "porte",
    "ente_federativo_responsavel",
)
"""As sete colunas de Empresas. Ver docs/layout_rfb.md."""

COLUNAS_SOCIOS: Final = (
    "cnpj_basico",
    "identificador_socio",
    # Nome de pessoa física na maioria dos registros. Mesma regra da razão social
    # de Empresas: o bronze mantém, o artefato publicado nunca contém.
    "nome_socio_ou_razao_social",
    "cnpj_cpf_socio",
    "qualificacao_socio",
    "data_entrada_sociedade",
    "pais",
    "representante_legal",
    "nome_representante",
    "qualificacao_representante_legal",
    "faixa_etaria",
)
"""As onze colunas de Socios, origem das arestas do grafo. Ver docs/layout_rfb.md."""


@dataclass(frozen=True)
class Tabela:
    """Um arquivo da Receita e o layout que o descreve."""

    nome: str
    """Nome da tabela na camada bronze, em minúsculas."""

    prefixo: str
    """Prefixo do arquivo extraído, como a Receita o nomeia."""

    colunas: tuple[str, ...]


ESTABELECIMENTOS: Final = Tabela("estabelecimentos", "Estabelecimentos", COLUNAS_ESTABELECIMENTOS)
EMPRESAS: Final = Tabela("empresas", "Empresas", COLUNAS_EMPRESAS)
SOCIOS: Final = Tabela("socios", "Socios", COLUNAS_SOCIOS)

TABELAS_PRINCIPAIS: Final = (ESTABELECIMENTOS, EMPRESAS, SOCIOS)

# O Parquet destes dados sai bem menor que o CSV, mas a checagem de espaço não pode
# depender disso: assumir que ele cabe no mesmo tamanho da entrada é o limite
# superior seguro, e a proporção real fica registrada no log de cada conversão.
FATOR_PARQUET_PESSIMISTA: Final = 1.0


class ErroDeBronze(RuntimeError):
    """Falha ao converter a camada bronze."""


class ContagemDivergenteError(ErroDeBronze):
    """O Parquet não tem a mesma quantidade de registros que o CSV de origem."""


class EspacoInsuficienteError(ErroDeBronze):
    """Não há espaço em disco para o Parquet."""


def abrir_conexao(config: Config, temporarios: Path) -> duckdb.DuckDBPyConnection:
    """Conexão com o teto de memória e o diretório de transbordo declarados.

    O transbordo em disco é o que permite ordenar e agregar um arquivo maior que a
    memória disponível. Sem diretório declarado, o DuckDB usa o do sistema, que num
    contêiner costuma ser pequeno e transforma falta de memória em erro obscuro.
    """
    temporarios.mkdir(parents=True, exist_ok=True)
    conexao = duckdb.connect()
    conexao.execute(f"SET memory_limit='{config.limite_de_memoria}'")
    conexao.execute(f"SET temp_directory='{temporarios.as_posix()}'")
    logger.info(
        "conexão do DuckDB configurada",
        extra={
            "limite_de_memoria": config.limite_de_memoria,
            "temp_directory": str(temporarios),
        },
    )
    return conexao


def _fonte(caminho: Path, colunas: tuple[str, ...]) -> str:
    """Cláusula de leitura com nome e tipo de cada coluna, sem sniffer."""
    declaracao = ", ".join(f"'{nome}': 'VARCHAR'" for nome in colunas)
    return (
        f"read_csv('{caminho.as_posix()}', delim=';', quote='\"', escape='\"', "
        f"header=false, encoding='{CODIFICACAO_DA_ENTRADA}', "
        f"columns={{{declaracao}}})"
    )


def contar_registros(
    conexao: duckdb.DuckDBPyConnection, caminho: Path, colunas: tuple[str, ...]
) -> int:
    resultado = conexao.execute(f"SELECT count(*) FROM {_fonte(caminho, colunas)}").fetchone()
    if resultado is None:  # pragma: no cover - o count sempre devolve uma linha
        raise ErroDeBronze(f"contagem de {caminho.name} não devolveu resultado")
    return int(resultado[0])


def verificar_espaco(destino: Path, tamanho_da_entrada: int) -> None:
    """Recusa antes de converter quando o disco não comporta o Parquet."""
    necessario = int(tamanho_da_entrada * FATOR_PARQUET_PESSIMISTA)
    livre = shutil.disk_usage(destino).free
    if necessario <= livre:
        logger.info(
            "espaço conferido antes da conversão",
            extra={"necessario_bytes": necessario, "livre_bytes": livre},
        )
        return

    faltam = necessario - livre
    raise EspacoInsuficienteError(
        f"A conversão pode precisar de até {necessario / 1024**3:.2f} GiB — o tamanho da "
        f"entrada, que é o limite superior seguro para o Parquet — e há "
        f"{livre / 1024**3:.2f} GiB livres em {destino}. "
        f"Faltam {faltam / 1024**3:.2f} GiB ({faltam:,} bytes)."
    )


def converter_para_parquet(
    conexao: duckdb.DuckDBPyConnection,
    origem: Path,
    destino: Path,
    colunas: tuple[str, ...],
) -> tuple[int, int]:
    """Converte um CSV em Parquet e devolve (registros, bytes do Parquet).

    A contagem do CSV é feita antes e a do Parquet depois. Divergir aqui significa
    que o parser leu o arquivo de um jeito e gravou de outro, e o único desfecho
    aceitável é interromper.
    """
    registros_na_origem = contar_registros(conexao, origem, colunas)

    parcial = destino.with_name(f"{destino.name}.parcial")
    conexao.execute(
        f"COPY (SELECT * FROM {_fonte(origem, colunas)}) "
        f"TO '{parcial.as_posix()}' (FORMAT PARQUET, COMPRESSION ZSTD)"
    )

    resultado = conexao.execute(
        f"SELECT count(*) FROM read_parquet('{parcial.as_posix()}')"
    ).fetchone()
    registros_no_parquet = int(resultado[0]) if resultado else -1

    if registros_no_parquet != registros_na_origem:
        parcial.unlink(missing_ok=True)
        raise ContagemDivergenteError(
            f"{origem.name} tem {registros_na_origem:,} registros e o Parquet saiu com "
            f"{registros_no_parquet:,}. A camada bronze não pode perder nem inventar "
            "registro: confira o tratamento de aspas e de quebra de linha dentro de campo."
        )

    parcial.replace(destino)
    return registros_no_parquet, destino.stat().st_size


def converter_tabela(config: Config, tabela: Tabela, competencia: str | None = None) -> list[Path]:
    """Converte todas as partições de uma tabela da competência.

    Os três arquivos principais diferem apenas no layout, e é por isso que só as
    colunas mudam entre eles. O tratamento — nada inferido, tudo texto, contagem
    conferida antes e depois — é o mesmo, e precisa continuar sendo: divergir por
    tabela é como uma delas passa a perder registro sem ninguém notar.
    """
    alvo = competencia or config.competencia
    entrada = config.data_dir / "extraido" / alvo
    destino = config.data_dir / "bronze" / alvo
    destino.mkdir(parents=True, exist_ok=True)

    origens = sorted(entrada.glob(f"{tabela.prefixo}*.csv"))
    if not origens:
        raise ErroDeBronze(
            f"Nenhum CSV de {tabela.prefixo} em {entrada}. Rode a ingestão antes do bronze."
        )

    verificar_espaco(destino, sum(caminho.stat().st_size for caminho in origens))

    gerados: list[Path] = []
    with abrir_conexao(config, config.data_dir / "duckdb-tmp") as conexao:
        for origem in origens:
            saida = destino / f"{origem.stem.lower()}.parquet"
            registros, bytes_do_parquet = converter_para_parquet(
                conexao, origem, saida, tabela.colunas
            )
            bytes_da_origem = origem.stat().st_size
            logger.info(
                "partição convertida",
                extra={
                    "tabela": tabela.nome,
                    "origem": origem.name,
                    "arquivo": saida.name,
                    "registros": registros,
                    "bytes_csv": bytes_da_origem,
                    "bytes_parquet": bytes_do_parquet,
                    "proporcao": round(bytes_do_parquet / bytes_da_origem, 4),
                },
            )
            gerados.append(saida)

    return gerados


def converter_estabelecimentos(config: Config, competencia: str | None = None) -> list[Path]:
    """Converte as dez partições de Estabelecimentos."""
    return converter_tabela(config, ESTABELECIMENTOS, competencia)


def converter_empresas(config: Config, competencia: str | None = None) -> list[Path]:
    """Converte as dez partições de Empresas.

    O Parquet resultante contém CPF sem máscara dentro de `razao_social`, para os
    registros de empresário individual. Ele é um artefato **local**, mora em
    `data/` e não é publicado em lugar nenhum. Quem for expor qualquer coisa
    derivada daqui parte do silver, onde a redação já aconteceu.
    """
    return converter_tabela(config, EMPRESAS, competencia)


def converter_socios(config: Config, competencia: str | None = None) -> list[Path]:
    """Converte as dez partições de Socios, de onde saem as arestas do grafo.

    Contém nome de pessoa física e CPF mascarado. Vale a mesma regra de Empresas:
    artefato local, nunca publicado.
    """
    return converter_tabela(config, SOCIOS, competencia)


def converter_principais(config: Config, competencia: str | None = None) -> dict[str, list[Path]]:
    """Converte Estabelecimentos, Empresas e Socios."""
    return {
        tabela.nome: converter_tabela(config, tabela, competencia) for tabela in TABELAS_PRINCIPAIS
    }
