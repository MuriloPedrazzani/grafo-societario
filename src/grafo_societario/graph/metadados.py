"""Converte `nos.parquet` no catálogo que o serving lê sem Parquet.

Este módulo é de **construção**: ele importa DuckDB. Quem lê o resultado é
`graph.catalogo`, que importa NumPy e `zlib` e mais nada — a mesma separação de
`components` e `csr`, e pelo mesmo motivo.

## O que sai daqui

| arquivo | o que é |
|---|---|
| `cnpj_ordenado.npy` | `cnpj_basico` das empresas, int32 ordenado |
| `no_por_cnpj.npy` | o índice de nó de cada um, paralelo ao anterior |
| `atributos.npy` | tipo, confiança e recorte empacotados em um int8 |
| `regiao_fiscal.npy` | o dígito, int8, com `-1` para quem não tem CPF |
| `nome_offsets.npy` | fronteiras do nome de cada nó no fluxo descomprimido |
| `bloco_inicio.npy` | onde cada bloco começa no fluxo descomprimido |
| `bloco_byte.npy` | onde cada bloco começa no blob comprimido |
| `nomes.bin` | os blocos `zlib` concatenados |

## Nenhum nome atravessa fronteira de bloco

O corte é feito **entre nomes**, e não a cada 64 KiB exatos. Isso troca alguns
bytes de bloco irregular por uma leitura que é uma descompressão e um recorte, em
vez de duas descompressões e uma emenda — e emenda é onde erro de fronteira se
esconde sem mudar contagem.

## A ida e volta é conferida sobre o dado inteiro

Todo nome gravado é lido de volta e comparado com a origem, nos 5,02 milhões, e
não numa amostra. É barato, roda uma vez na construção, e é a única prova de que
os deslocamentos apontam para o que dizem apontar. Array paralelo desalinhado não
muda contagem nenhuma — muda só o significado, e sai plausível.
"""

from __future__ import annotations

import logging
import zlib
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Final

import numpy as np

from grafo_societario.config import Config
from grafo_societario.graph.artefatos import TIPOS
from grafo_societario.graph.catalogo import CONFIANCAS, ESTIMADA, REGIOES, SEM_REGIAO
from grafo_societario.transform.bronze import abrir_conexao

logger = logging.getLogger(__name__)

BLOCO: Final = 64 * 1024
"""Alvo de bytes descomprimidos por bloco.

Medido sobre os 178,8 MB de razão social: 32 KiB dão 66,9 MB, 64 KiB dão 63,6 MB
e 256 KiB dão 60,8 MB. O ganho de 256 KiB sobre 64 KiB é de 4,4%, e o custo é
quadruplicar o que se descomprime para ler um nome. 64 KiB fica abaixo dos 67,0 MB
que o ZSTD do Parquet faz na mesma coluna.
"""

NIVEL: Final = 6
"""Nível do zlib. O padrão da biblioteca, e o que produziu os 63,6 MB medidos."""

LOTE: Final = 200_000
"""Linhas por busca ao motor.

Trazer os 10,6 milhões de nomes de uma vez custaria mais de um giga em objetos
Python. O lote mantém a memória limitada sem exigir uma passagem por linha.
"""


class ErroDeMetadados(RuntimeError):
    """Falha ao converter os metadados dos nós."""


class NosAusentesError(ErroDeMetadados):
    """A conversão foi pedida antes de os nós existirem."""


class NomeDivergenteError(ErroDeMetadados):
    """Um nome lido de volta não é o que foi gravado."""


class TaxaDivergenteError(ErroDeMetadados):
    """A taxa derivada da região não reproduz a que a camada de identidade calculou."""


@dataclass(frozen=True)
class Metadados:
    """O que a conversão produziu."""

    destino: Path
    nos: int
    empresas: int
    nomes: int
    blocos: int
    bytes_dos_nomes: int
    bytes_totais: int


def _empacotar(tipo: str, confianca: str, no_recorte: bool | None) -> int:
    """Tipo, confiança e recorte num int8: 2 bits, 2 bits e 2 bits.

    Cabem em um byte com folga, e um byte por nó são 10,7 MB — contra 32,1 MB se
    cada um fosse um array. O recorte usa dois bits porque ele é ternário: nulo
    para quem não é empresa, e verdadeiro ou falso para quem é.
    """
    marca = 0 if no_recorte is None else (2 if no_recorte else 1)
    return TIPOS.index(tipo) | (CONFIANCAS.index(confianca) << 2) | (marca << 4)


def _tabela_de_taxas(
    regioes: np.ndarray[Any, np.dtype[np.int8]], taxas: np.ndarray[Any, np.dtype[np.float64]]
) -> np.ndarray[Any, np.dtype[np.float64]]:
    """Os dez valores de taxa de colisão, **indexados pelo dígito da região**.

    A posição *é* o dígito, e não a ordem de aparição. Guardar só os dígitos
    presentes economizaria bytes que não existem — são oitenta — e faria a região
    0 virar a região 1 no primeiro mês em que um dígito não aparecesse, sem nada
    falhar.

    Levanta se algum dígito tiver mais de uma taxa: **a derivação inteira depende
    de a taxa ser função da região**, e isso é conferido aqui, não suposto.
    """
    tabela = np.full(REGIOES, np.nan, dtype=np.float64)
    conhecidas = ~np.isnan(taxas)
    for digito in range(REGIOES):
        candidatas = np.unique(taxas[conhecidas & (regioes == digito)])
        if candidatas.size > 1:
            raise TaxaDivergenteError(
                f"A região {digito} tem {candidatas.size} taxas distintas: "
                f"{', '.join(f'{c:.6g}' for c in candidatas[:5])}. A taxa é calculada por região "
                "na camada de identidade, e derivá-la da região só é válido enquanto ela for "
                "função dela."
            )
        if candidatas.size == 1:
            tabela[digito] = candidatas[0]
    return tabela


def _conferir_taxa(
    tabela: np.ndarray[Any, np.dtype[np.float64]],
    regioes: np.ndarray[Any, np.dtype[np.int8]],
    aplica: np.ndarray[Any, np.dtype[np.bool_]],
    esperado: np.ndarray[Any, np.dtype[np.float64]],
) -> None:
    """Reproduz a taxa de **cada** nó a partir dos dez valores e exige igualdade.

    Não é amostra: são os 5,6 milhões de nós de pessoa física, e a comparação é
    vetorizada, então custa uma passagem numa etapa que roda uma vez. É o que
    separa uma derivação **fiel** de uma derivação plausível — e plausível é
    exatamente o que passa despercebido, porque a contagem de nós não muda.

    `aplica` é o mesmo predicado que o catálogo usa para decidir se há taxa
    (`confianca == estimada`), de propósito: uma guarda que confere uma regra
    diferente da que o leitor executa não confere nada.
    """
    derivado = np.where(aplica & (regioes >= 0), tabela[np.maximum(regioes, 0)], np.nan)
    fiel = (np.isnan(derivado) & np.isnan(esperado)) | (derivado == esperado)
    if not bool(fiel.all()):
        divergentes = int((~fiel).sum())
        primeiro = int(np.argmax(~fiel))
        raise TaxaDivergenteError(
            f"{divergentes:,} nós têm taxa diferente da que a região deriva. O primeiro é o nó "
            f"{primeiro:,}: região {int(regioes[primeiro])}, gravado {esperado[primeiro]!r}, "
            f"derivado {derivado[primeiro]!r}. A tabela por região não descreve este artefato."
        )


def _blocos_de_nomes(
    nomes: list[bytes],
) -> tuple[bytes, np.ndarray[Any, np.dtype[np.int32]], np.ndarray[Any, np.dtype[np.int64]]]:
    """Comprime a sequência de nomes em blocos que nenhum nome atravessa."""
    partes: list[bytes] = []
    inicios: list[int] = [0]
    bytes_do_bloco: list[int] = [0]
    buffer: list[bytes] = []
    tamanho = 0
    descomprimido = 0

    def fechar() -> None:
        nonlocal tamanho, descomprimido
        if not buffer:
            return
        comprimido = zlib.compress(b"".join(buffer), NIVEL)
        partes.append(comprimido)
        descomprimido += tamanho
        inicios.append(descomprimido)
        bytes_do_bloco.append(bytes_do_bloco[-1] + len(comprimido))
        buffer.clear()
        tamanho = 0

    for nome in nomes:
        if tamanho and tamanho + len(nome) > BLOCO:
            fechar()
        buffer.append(nome)
        tamanho += len(nome)
    fechar()

    return (
        b"".join(partes),
        np.array(inicios, dtype=np.int32),
        np.array(bytes_do_bloco, dtype=np.int64),
    )


def serializar_metadados(config: Config, competencia: str | None = None) -> Metadados:
    """Lê `nos.parquet` e grava o catálogo que o serving consome.

    A leitura é ordenada por `file_row_number`, que é a posição física da linha —
    a mesma que define o índice do nó. Ordenar por qualquer outra coisa produziria
    arrays válidos apontando para os nós errados.
    """
    alvo = competencia or config.competencia
    destino = config.data_dir / "grafo" / alvo
    nos_parquet = destino / "nos.parquet"
    if not nos_parquet.exists():
        raise NosAusentesError(
            f"Não há nós em {nos_parquet}. O catálogo é a conversão deles para o formato que a "
            "resposta lê sem Parquet."
        )

    with abrir_conexao(config, config.data_dir / "duckdb-tmp") as conexao:
        fonte = (
            f"read_parquet('{nos_parquet.as_posix()}', file_row_number = true) "
            "ORDER BY file_row_number"
        )
        colunas = conexao.execute(
            "SELECT tipo, confianca, no_recorte, regiao_fiscal, cnpj_basico, taxa_de_colisao "
            f"FROM {fonte}"
        ).fetchall()

        atributos = np.fromiter(
            (_empacotar(str(t), str(c), r) for t, c, r, _, _, _ in colunas),
            dtype=np.int8,
            count=len(colunas),
        )
        regiao = np.fromiter(
            (SEM_REGIAO if d is None else int(d) for _, _, _, d, _, _ in colunas),
            dtype=np.int8,
            count=len(colunas),
        )
        # A taxa vem por nó no artefato interno e sai por região no publicável.
        # `aplica` é o predicado do catálogo, não uma reescrita dele.
        taxa_por_no = np.fromiter(
            (np.nan if x is None else float(x) for *_, x in colunas),
            dtype=np.float64,
            count=len(colunas),
        )
        aplica = np.fromiter(
            (c == ESTIMADA for _, c, *_ in colunas), dtype=bool, count=len(colunas)
        )
        taxa_por_regiao = _tabela_de_taxas(regiao, taxa_por_no)
        _conferir_taxa(taxa_por_regiao, regiao, aplica, taxa_por_no)

        empresas = [
            (int(cnpj), posicao)
            for posicao, (_, _, _, _, cnpj, _) in enumerate(colunas)
            if cnpj is not None
        ]
        empresas.sort()
        cnpj_ordenado = np.array([cnpj for cnpj, _ in empresas], dtype=np.int32)
        no_por_cnpj = np.array([posicao for _, posicao in empresas], dtype=np.int32)
        del colunas, empresas

        # Os nomes vêm em lote: os 10,6 milhões de uma vez custariam mais de um
        # giga em objetos Python, e a conversão roda na mesma máquina de 8 GiB.
        cursor = conexao.execute(f"SELECT nome FROM {fonte}")
        nomes: list[bytes] = []
        while lote := cursor.fetchmany(LOTE):
            nomes.extend((linha[0] or "").encode("utf-8") for linha in lote)

    offsets = np.zeros(len(nomes) + 1, dtype=np.int32)
    offsets[1:] = np.cumsum([len(nome) for nome in nomes], dtype=np.int64)
    blob, bloco_inicio, bloco_byte = _blocos_de_nomes(nomes)

    _conferir_ida_e_volta(nomes, offsets, blob, bloco_inicio, bloco_byte)

    escritos: dict[str, np.ndarray[Any, np.dtype[Any]]] = {
        "cnpj_ordenado.npy": cnpj_ordenado,
        "no_por_cnpj.npy": no_por_cnpj,
        "atributos.npy": atributos,
        "regiao_fiscal.npy": regiao,
        "taxa_por_regiao.npy": taxa_por_regiao,
        "nome_offsets.npy": offsets,
        "bloco_inicio.npy": bloco_inicio,
        "bloco_byte.npy": bloco_byte,
    }
    total = 0
    for nome, array in escritos.items():
        parcial = destino / f"{nome}.parcial"
        with parcial.open("wb") as arquivo:
            np.save(arquivo, array, allow_pickle=False)
        parcial.replace(destino / nome)
        total += (destino / nome).stat().st_size
    parcial_do_blob = destino / "nomes.bin.parcial"
    parcial_do_blob.write_bytes(blob)
    parcial_do_blob.replace(destino / "nomes.bin")
    total += len(blob)

    resultado = Metadados(
        destino=destino,
        nos=int(atributos.size),
        empresas=int(cnpj_ordenado.size),
        nomes=sum(1 for nome in nomes if nome),
        blocos=int(bloco_inicio.size) - 1,
        bytes_dos_nomes=len(blob),
        bytes_totais=total,
    )
    logger.info(
        "catálogo de nós serializado",
        extra={
            "competencia": alvo,
            "nos": resultado.nos,
            "empresas": resultado.empresas,
            "nomes": resultado.nomes,
            "blocos": resultado.blocos,
            "bytes_nomes": resultado.bytes_dos_nomes,
            "bytes_totais": resultado.bytes_totais,
        },
    )
    return resultado


def _conferir_ida_e_volta(
    nomes: list[bytes],
    offsets: np.ndarray[Any, np.dtype[np.int32]],
    blob: bytes,
    bloco_inicio: np.ndarray[Any, np.dtype[np.int32]],
    bloco_byte: np.ndarray[Any, np.dtype[np.int64]],
) -> None:
    """Lê **todo** nome de volta e exige que seja igual ao da origem.

    Não é amostra. Deslocamento errado por um byte devolve nome de outra empresa e
    não muda contagem nenhuma — a única forma de ver isso é comparar tudo, e
    comparar tudo custa uma passagem numa etapa que roda uma vez.
    """
    for bloco in range(bloco_inicio.size - 1):
        aberto = zlib.decompress(blob[int(bloco_byte[bloco]) : int(bloco_byte[bloco + 1])])
        base = int(bloco_inicio[bloco])
        if len(aberto) != int(bloco_inicio[bloco + 1]) - base:
            raise NomeDivergenteError(
                f"O bloco {bloco:,} descomprime {len(aberto):,} bytes e o índice diz "
                f"{int(bloco_inicio[bloco + 1]) - base:,}. O blob e o índice de blocos "
                "discordam, e cada nome depois daqui sai deslocado."
            )
        primeiro = int(np.searchsorted(offsets, base, side="left"))
        for no in range(primeiro, offsets.size - 1):
            inicio, fim = int(offsets[no]), int(offsets[no + 1])
            if inicio >= int(bloco_inicio[bloco + 1]):
                break
            lido = aberto[inicio - base : fim - base]
            if lido != nomes[no]:
                raise NomeDivergenteError(
                    f"O nó {no:,} gravou {nomes[no]!r} e devolveu {lido!r}. Os deslocamentos e "
                    "o blob estão desalinhados, e a resposta sai com o nome de outra empresa."
                )
