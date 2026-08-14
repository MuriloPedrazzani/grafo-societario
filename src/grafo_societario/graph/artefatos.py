"""O que é publicado, e como se identifica o build que está no ar.

Vive num módulo próprio porque tem **dois consumidores em lados opostos da
fronteira**: o benchmark, que mede o orçamento de deploy, e o `/health`, que
declara qual build está respondendo. O benchmark importa DuckDB e psutil; a API
não pode importar nenhum dos dois.

Aqui não se importa nada além da biblioteca padrão.
"""

from __future__ import annotations

import hashlib
from pathlib import Path
from typing import Final

BLOCO_DE_LEITURA: Final = 1024 * 1024
"""Um MiB por leitura. Medido: 1,5 GB/s sobre os 416 MB do artefato."""

ARTEFATOS_PUBLICAVEIS: Final = (
    "cnpj_ordenado.npy",
    "no_por_cnpj.npy",
    "atributos.npy",
    "regiao_fiscal.npy",
    "nome_offsets.npy",
    "bloco_inicio.npy",
    "bloco_byte.npy",
    "nomes.bin",
    "existencia.npy",
    "indptr.npy",
    "indices.npy",
    "qualificacoes.npy",
    "componentes.npy",
)
"""O que vai para GitHub Release e para a imagem.

**Lista de permissão, e não de exclusão** — sair é o padrão, entrar é decisão. Três
arquivos ficam de fora e cada um por um motivo diferente: `arestas.parquet` é
insumo do CSR, `identificadores.parquet` carrega a chave reversível de pessoa
física, e `nos.parquet` foi substituído pelo catálogo, porque a resposta não lê
Parquet.
"""


def soma_do_arquivo(caminho: Path) -> str:
    """SHA-256 de um artefato, lido em blocos."""
    digestor = hashlib.sha256()
    with caminho.open("rb") as arquivo:
        while pedaco := arquivo.read(BLOCO_DE_LEITURA):
            digestor.update(pedaco)
    return digestor.hexdigest()


def somas_dos_artefatos(origem: Path) -> dict[str, str]:
    """SHA-256 de cada artefato publicável presente em `origem`.

    É o que responde "qual build está no ar" sem adivinhação: a competência diz de
    qual mês é o dado, e a soma diz qual execução o produziu. Duas construções da
    mesma competência sobre o mesmo silver dão a mesma soma — o projeto garante
    isso desde a Fase 4 —, então soma diferente é dado diferente, e não ruído.
    """
    return {
        nome: soma_do_arquivo(origem / nome)
        for nome in ARTEFATOS_PUBLICAVEIS
        if (origem / nome).exists()
    }
