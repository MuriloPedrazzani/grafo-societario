"""Os nós do grafo, e a fronteira entre estar no grafo e existir.

## Nem toda empresa do recorte é um nó do grafo

Das 19.770.618 empresas do recorte de SP, **14.791.390 não têm nenhuma aresta** —
74,8%. São, quase todas, empresário individual: o dono está dentro da razão social
e o projeto recusa extraí-lo de lá, então nenhum vínculo é registrado. Ver a
decisão e o custo em `transform.silver`.

Um nó de grau zero não pode estar em caminho societário nenhum. Carregá-lo no CSR
custa uma entrada de `indptr` e uma linha de metadados por nada, e é o que estoura
o orçamento de 500 MB do artefato — o gargalo do deploy são os metadados dos nós,
não os arrays do grafo.

## Mas "não tem vínculo" e "não existe" são respostas diferentes

Deixar os isolados de fora do CSR não pode transformar uma consulta sobre eles em
"empresa não encontrada". Isso seria falso: a empresa existe, está no recorte, e a
resposta certa é que ela não tem vínculo societário registrado.

Daí a separação em dois artefatos:

- **`nos.parquet`** — os 10.658.250 nós com pelo menos uma aresta, com o índice
  denso e os atributos de cada um. É o dicionário reverso do grafo.
- **`existencia.npy`** — os 19.770.618 `cnpj_basico` do recorte, como int32
  ordenado. Responde existência por busca binária, **exatamente**: sem falso
  positivo, ao contrário de um filtro probabilístico, e sem carregar metadado de
  quem não tem vínculo.

O `cnpj_basico` tem oito dígitos e vai até 98.669.773 — cabe em int32 com folga de
vinte e uma vezes. São 75,4 MiB, contra 150,8 MiB em int64. O zero à esquerda se
recupera com preenchimento na leitura; o valor não se perde.

## O índice denso é interno, e nunca sai na resposta

O identificador público de um nó é o **CNPJ** ou o **hash de identidade**. O índice
0..N-1 existe para endereçar posição em array e não significa nada fora desta
competência: ele é atribuído pela ordem do identificador, e o conjunto de nós muda
todo mês.

Uma rota do tipo `/no/12345` funcionaria hoje e devolveria **outra empresa** no mês
seguinte — sem erro, sem aviso, com aparência de resposta correta. É o mesmo modo
de falha do preenchedor de representante: não uma exceção, um resultado plausível e
falso. O índice não atravessa a fronteira da API.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Final

import duckdb
import numpy as np

from grafo_societario.config import Config
from grafo_societario.transform.bronze import abrir_conexao
from grafo_societario.transform.identity import TIPOS, instalar_identificador

logger = logging.getLogger(__name__)

COLUNAS_NOS: Final = (
    "indice",
    "identificador",
    "tipo",
    "nome",
    "cnpj_basico",
    "cpf_mascarado",
    "pais",
    "no_recorte",
    "confianca",
    "taxa_de_colisao",
)
"""O dicionário reverso: de índice denso para o que o nó é.

`indice` é INTEGER, não BIGINT. São 10.658.250 nós contra um teto de 2.147.483.647,
folga de duzentas vezes — e o dobro de largura custaria 40 MiB no `indptr` sem
comprar nada. A decisão está medida, não presumida.
"""

TIPO_DE_EMPRESA: Final = TIPOS["1"]


class ErroDeGrafo(RuntimeError):
    """Falha ao construir o grafo."""


class SilverAusenteError(ErroDeGrafo):
    """A construção foi pedida antes de a camada silver existir."""


class IndiceNaoDensoError(ErroDeGrafo):
    """O índice não cobre exatamente 0..N-1."""


class ExistenciaDesordenadaError(ErroDeGrafo):
    """O array de existência não está ordenado, e a busca binária mentiria."""


@dataclass(frozen=True)
class Nos:
    """O que a geração produziu."""

    caminho: Path
    caminho_da_existencia: Path

    nos: int
    """Nós com pelo menos uma aresta. São os que entram no CSR."""

    por_tipo: tuple[tuple[str, int], ...]

    existencia: int
    """Empresas do recorte cuja existência é respondível — todas, com ou sem vínculo."""

    isolados: int
    """Empresas do recorte sem nenhuma aresta. Ficam fora do grafo e dentro da
    existência: consultá-las devolve "sem vínculo", nunca "não existe"."""

    bytes_dos_nos: int
    bytes_da_existencia: int


def validar_indice_denso(conexao: duckdb.DuckDBPyConnection, caminho: Path) -> None:
    """Recusa índice que não cubra exatamente 0..N-1.

    O índice é endereço de posição em array. Buraco faz o CSR apontar para posição
    que não existe; repetição faz dois nós dividirem a mesma lista de vizinhos. Nos
    dois casos o erro aparece como caminho societário errado, não como exceção.
    """
    medida = conexao.execute(
        f"SELECT count(*), min(indice), max(indice), count(DISTINCT indice) "
        f"FROM read_parquet('{caminho.as_posix()}')"
    ).fetchone()
    quantos, menor, maior, distintos = (
        tuple(int(valor) for valor in medida) if medida else (0, -1, -1, 0)
    )
    if (menor, maior, distintos) != (0, quantos - 1, quantos):
        raise IndiceNaoDensoError(
            f"O índice precisa cobrir 0..{quantos - 1:,} sem buraco nem repetição, e saiu com "
            f"menor={menor}, maior={maior}, distintos={distintos:,}. Índice esparso faz o CSR "
            "endereçar posição que não existe."
        )


def validar_existencia_ordenada(existencia: np.ndarray[Any, np.dtype[np.int32]]) -> None:
    """Recusa array de existência fora de ordem estritamente crescente.

    A busca binária sobre array desordenado não erra em voz alta: ela devolve "não
    existe" para quem existe. É o modo de falha que nenhum teste de contagem pega,
    porque o tamanho do array continua certo.
    """
    if existencia.size and not bool(np.all(existencia[:-1] < existencia[1:])):
        raise ExistenciaDesordenadaError(
            "O array de existência precisa estar em ordem estritamente crescente: a busca "
            "binária sobre array desordenado não erra em voz alta, devolve 'não existe' para "
            "quem existe."
        )


def _caminhos(config: Config, competencia: str) -> tuple[Path, Path, Path]:
    silver = config.data_dir / "silver" / competencia
    faltando = [
        nome
        for nome in ("recorte", "empresas", "socios", "identidades")
        if not (silver / f"{nome}.parquet").exists()
    ]
    if faltando:
        raise SilverAusenteError(
            f"Faltam artefatos do silver em {silver}: {', '.join(faltando)}. O grafo é "
            "construído a partir deles, e não do bronze."
        )
    return silver / "recorte.parquet", silver / "empresas.parquet", silver / "identidades.parquet"


def gerar_nos(config: Config, competencia: str | None = None) -> Nos:
    """Mapeia cada nó para um inteiro denso e grava o dicionário reverso.

    Um nó entra se tiver pelo menos uma aresta, de qualquer um dos dois lados: a
    empresa que tem sócio, e o sócio — que pode ser pessoa física, estrangeiro, ou
    outra empresa, dentro ou fora do recorte.

    A distinção entre os dois lados não é simétrica no dado: 1.311 empresas do
    recorte **não têm sócio nenhum e mesmo assim têm aresta**, porque são sócias de
    outra empresa. Contar apenas quem tem sócio as deixaria de fora do grafo com
    grau aparente zero, e elas têm vínculo.

    **A ordem é total e explícita.** O índice sai da ordenação por `identificador`,
    que é único por construção — duas execuções sobre o mesmo silver produzem o
    mesmo índice, byte a byte. Sem isso, tudo o que vem depois muda de significado
    entre execuções, e o artefato deixa de ser imutável.
    """
    alvo = competencia or config.competencia
    recorte, empresas, identidades = _caminhos(config, alvo)
    socios = recorte.with_name("socios.parquet")

    destino = config.data_dir / "grafo" / alvo
    destino.mkdir(parents=True, exist_ok=True)
    nos_parquet = destino / "nos.parquet"
    existencia_npy = destino / "existencia.npy"

    with abrir_conexao(config, config.data_dir / "duckdb-tmp") as conexao:
        instalar_identificador(conexao)
        # Sem isto o motor mantém buffer para devolver as linhas na ordem em que as
        # leu, e 10,6 milhões de nós não cabem em 4 GB desse jeito. É seguro aqui
        # porque nenhuma saída depende de ordem de leitura: toda a que importa tem
        # `ORDER BY` explícito, e o determinismo é conferido por SHA-256.
        conexao.execute("SET preserve_insertion_order=false")

        # Quais empresas do recorte têm aresta, por `cnpj_basico` — que é junção de
        # string e barata. Calcular o hash das 19,77 milhões para depois filtrar não
        # cabe em 4 GB, e não precisa: o filtro vem antes.
        conexao.execute(
            f"""
            CREATE OR REPLACE TEMP TABLE empresa_com_aresta AS
            SELECT r.cnpj_basico FROM read_parquet('{recorte.as_posix()}') r
            SEMI JOIN (
              SELECT cnpj_basico FROM read_parquet('{socios.as_posix()}')
              UNION
              SELECT substr(cnpj_cpf_socio, 1, 8) FROM read_parquet('{socios.as_posix()}')
              WHERE identificador_socio = '1'
            ) lado USING (cnpj_basico)
            """
        )

        # A razão social de empresas é a autoritativa: ela passou pela tipagem do
        # silver, enquanto o nome que vem de Socios é a grafia de quem preencheu.
        conexao.execute(
            f"""
            CREATE OR REPLACE TEMP TABLE empresa_no AS
            SELECT identificador(['{TIPO_DE_EMPRESA}', e.cnpj_basico]) AS identificador,
                   e.razao_social AS nome,
                   e.cnpj_basico
            FROM read_parquet('{empresas.as_posix()}') e
            SEMI JOIN empresa_com_aresta a USING (cnpj_basico)
            """
        )

        conexao.execute("DROP TABLE empresa_com_aresta")

        # O universo de nós sai de um FULL OUTER JOIN, e não de uma união seguida de
        # duas junções: os dois lados são exatamente as empresas do recorte com
        # aresta e as identidades de sócio, que por construção só existem se houver
        # vínculo. Uma passagem em vez de três, e o dobro de memória economizado.
        parcial = nos_parquet.with_name(f"{nos_parquet.name}.parcial")
        conexao.execute(
            f"""
            COPY (
              SELECT CAST(row_number() OVER (ORDER BY identificador) - 1 AS INTEGER) AS indice, *
              FROM (
                SELECT
                  coalesce(e.identificador, i.identificador) AS identificador,
                  coalesce(i.tipo, '{TIPO_DE_EMPRESA}') AS tipo,
                  coalesce(e.nome, i.nome) AS nome,
                  coalesce(e.cnpj_basico, i.cnpj_basico) AS cnpj_basico,
                  i.cpf_mascarado,
                  i.pais,
                  CASE WHEN coalesce(i.tipo, '{TIPO_DE_EMPRESA}') = '{TIPO_DE_EMPRESA}'
                       THEN e.identificador IS NOT NULL OR coalesce(i.no_recorte, FALSE) END
                    AS no_recorte,
                  coalesce(i.confianca, 'exata') AS confianca,
                  i.taxa_de_colisao
                FROM empresa_no e
                FULL OUTER JOIN read_parquet('{identidades.as_posix()}') i
                  ON e.identificador = i.identificador
              )
              ORDER BY identificador
            ) TO '{parcial.as_posix()}' (FORMAT PARQUET, COMPRESSION ZSTD)
            """
        )

        try:
            validar_indice_denso(conexao, parcial)
        except ErroDeGrafo:
            parcial.unlink(missing_ok=True)
            raise
        medida = conexao.execute(
            f"SELECT count(*) FROM read_parquet('{parcial.as_posix()}')"
        ).fetchone()
        quantos = int(medida[0]) if medida else 0

        por_tipo = tuple(
            (str(nome), int(total))
            for nome, total in conexao.execute(
                f"SELECT tipo, count(*) FROM read_parquet('{parcial.as_posix()}') "
                "GROUP BY 1 ORDER BY 1"
            ).fetchall()
        )

        # Do recorte, e não de todo nó de pessoa jurídica: os 36.810 conectores de
        # fora também são pessoa jurídica e nunca estiveram no recorte, então
        # subtraí-los daqui contaria como isolada empresa que nem é nossa.
        do_recorte = conexao.execute(
            f"SELECT count(*) FROM read_parquet('{parcial.as_posix()}') "
            f"WHERE tipo = '{TIPO_DE_EMPRESA}' AND no_recorte"
        ).fetchone()
        empresas_com_aresta = int(do_recorte[0]) if do_recorte else 0

        # A existência é do recorte inteiro, e não só de quem tem vínculo.
        coluna = conexao.execute(
            f"SELECT CAST(cnpj_basico AS INTEGER) AS cnpj "
            f"FROM read_parquet('{recorte.as_posix()}') ORDER BY 1"
        ).fetchnumpy()["cnpj"]
        existencia = np.ascontiguousarray(coluna, dtype=np.int32)

    try:
        validar_existencia_ordenada(existencia)
    except ErroDeGrafo:
        parcial.unlink(missing_ok=True)
        raise

    parcial.replace(nos_parquet)
    np.save(existencia_npy, existencia, allow_pickle=False)

    isolados = int(existencia.size) - empresas_com_aresta
    resultado = Nos(
        caminho=nos_parquet,
        caminho_da_existencia=existencia_npy,
        nos=quantos,
        por_tipo=por_tipo,
        existencia=int(existencia.size),
        isolados=isolados,
        bytes_dos_nos=nos_parquet.stat().st_size,
        bytes_da_existencia=existencia_npy.stat().st_size,
    )
    logger.info(
        "nós indexados",
        extra={
            "competencia": alvo,
            "uf_alvo": config.uf_alvo,
            "nos": resultado.nos,
            "por_tipo": dict(por_tipo),
            "existencia": resultado.existencia,
            "isolados": resultado.isolados,
            "bytes_nos": resultado.bytes_dos_nos,
            "bytes_existencia": resultado.bytes_da_existencia,
        },
    )
    return resultado
