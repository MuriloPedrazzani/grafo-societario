"""Componentes conexos: quem alcança quem, respondido antes de qualquer travessia.

Dois nós no mesmo componente **podem** ter caminho entre si; dois nós em
componentes diferentes **não podem**, e isso se responde comparando dois inteiros
em vez de percorrer o grafo. É a resposta negativa mais barata que existe, e a
Fase 5 depende dela para não gastar uma busca bidirecional inteira para concluir
que não há o que buscar.

## O rótulo é canônico, e não o que o algoritmo devolveu

O `csgraph` numera os componentes pela ordem em que os encontra na travessia.
É determinístico, e é arbitrário: o número não significa nada. Aqui os rótulos
são reatribuídos por **tamanho decrescente**, com desempate pelo **menor índice de
nó**, e aí `componente 0` passa a querer dizer "o maior componente do grafo" em
vez de "o primeiro que a varredura topou".

A diferença aparece quando alguém publica um número. "O nó está no componente 0"
é uma frase verificável com a ordenação canônica, e é uma coincidência de
implementação sem ela — que muda de significado se a ordem de varredura mudar de
versão do scipy.

## Este módulo é de construção, e não de resposta

Ele importa scipy; nada no caminho de serving importa. O rótulo é calculado uma
vez, serializado em `componentes.npy`, e a API o lê com NumPy — ver
`graph.csr.carregar_componentes`. É o mesmo desenho que mantém `graph/csr.py`
sem DuckDB: a imagem que responde consulta não carrega a máquina que produziu o
artefato.

## Laço removido criou nó de grau zero dentro do CSR

A serialização descarta laço, e um nó cujo único vínculo era consigo mesmo fica
com grau zero **dentro** do CSR — ele é nó (tem aresta na lista) e não tem
vizinho (a aresta era um laço). Esses viram componente de tamanho 1, e o número
deles é contado à parte: é consequência de uma decisão da serialização, e não uma
propriedade do grafo societário.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Final

import numpy as np
from scipy.sparse import csr_matrix
from scipy.sparse.csgraph import connected_components

from grafo_societario.config import Config
from grafo_societario.graph.csr import Grafo, abrir_grafo

logger = logging.getLogger(__name__)

ARQUIVO: Final = "componentes.npy"

MAIORES_REPORTADOS: Final = 10
"""Quantos tamanhos vão no resultado e no log.

O maior sozinho não descreve a forma do grafo: a pergunta que decide a viabilidade
da travessia é se existe **um** gigante ou vários grandes, e isso só se vê
comparando o primeiro com os seguintes.
"""


class ErroDeComponentes(RuntimeError):
    """Falha ao calcular os componentes conexos."""


class RotuloNaoCanonicoError(ErroDeComponentes):
    """Os rótulos não estão em ordem de tamanho decrescente."""


@dataclass(frozen=True)
class Componentes:
    """O que o cálculo produziu, e o que a Fase 5 precisa saber antes de existir."""

    caminho: Path

    nos: int
    quantos: int

    maiores: tuple[int, ...]
    """Os maiores tamanhos, em ordem decrescente. `maiores[0]` é o gigante."""

    gigante: int
    fora_do_gigante: int
    """Nós que **não** estão no maior componente. É o universo em que uma consulta
    entre dois nós pode ter resposta negativa sem travessia nenhuma."""

    isolados_no_csr: int
    """Componentes de tamanho 1: nós que entraram na lista de arestas e ficaram
    sem vizinho depois do descarte de laço."""

    bytes_dos_componentes: int


def validar_rotulos_canonicos(
    rotulos: np.ndarray[Any, np.dtype[np.int32]],
    tamanhos: np.ndarray[Any, np.dtype[np.integer[Any]]],
) -> None:
    """Recusa rotulagem que não seja por tamanho decrescente, desempate por nó.

    A guarda existe porque a reordenação é fácil de escrever quase certa: uma
    ordenação estável trocada por instável, ou um `argsort` no lugar de um
    `lexsort`, produz rótulos que continuam **válidos** — cada componente ainda
    tem o seu número e a partição continua correta. Só o significado do número se
    perde, e nenhum teste de conectividade percebe.
    """
    if not tamanhos.size:
        return
    if not bool(np.all(tamanhos[:-1] >= tamanhos[1:])):
        raise RotuloNaoCanonicoError(
            "Os componentes precisam estar rotulados por tamanho decrescente: sem isso, "
            "'componente 0' é o primeiro que a varredura encontrou e não o maior do grafo, "
            "e o número deixa de significar alguma coisa fora desta execução."
        )
    primeiro = _primeira_ocorrencia(rotulos, int(tamanhos.size))
    empate = tamanhos[:-1] == tamanhos[1:]
    if empate.any() and not bool(np.all(primeiro[:-1][empate] < primeiro[1:][empate])):
        raise RotuloNaoCanonicoError(
            "Componentes de mesmo tamanho precisam ser desempatados pelo menor índice de nó. "
            "Sem o desempate a ordem entre eles fica a critério do algoritmo de ordenação, e "
            "o rótulo volta a ser sorteio onde há empate."
        )


def _primeira_ocorrencia(
    rotulos: np.ndarray[Any, np.dtype[np.int32]], quantos: int
) -> np.ndarray[Any, np.dtype[np.int64]]:
    """O menor índice de nó de cada rótulo, numa passagem.

    A escrita é feita de trás para frente de propósito: quando dois nós têm o
    mesmo rótulo, quem escreve por último vence, e percorrendo ao contrário o
    último a escrever é o de menor índice.
    """
    primeiro = np.empty(quantos, dtype=np.int64)
    primeiro[rotulos[::-1]] = np.arange(rotulos.size - 1, -1, -1, dtype=np.int64)
    return primeiro


def _matriz(grafo: Grafo) -> csr_matrix:
    """A mesma topologia, no tipo que o `csgraph` consome.

    Os arrays vêm do mapeamento e são somente leitura; `data` é o único que
    precisa existir, e vale 1 em toda posição porque o grafo não é ponderado.
    """
    return csr_matrix(
        (
            np.ones(grafo.posicoes, dtype=np.int8),
            np.asarray(grafo.indices),
            np.asarray(grafo.indptr),
        ),
        shape=(grafo.nos, grafo.nos),
        copy=False,
    )


def calcular_componentes(config: Config, competencia: str | None = None) -> Componentes:
    """Rotula cada nó com o seu componente e grava o array.

    O grafo é não direcionado, então componente fracamente e fortemente conexo são
    a mesma coisa aqui — `directed=False` diz isso ao `csgraph` e evita que ele
    faça o trabalho a mais de distinguir os dois.
    """
    alvo = competencia or config.competencia
    grafo = abrir_grafo(config, alvo)
    destino = config.data_dir / "grafo" / alvo / ARQUIVO

    quantos, bruto = connected_components(_matriz(grafo), directed=False, return_labels=True)
    quantos = int(quantos)

    tamanhos = np.bincount(bruto, minlength=quantos)
    primeiro = _primeira_ocorrencia(bruto.astype(np.int32), quantos)
    # `lexsort` usa a última chave como primária: tamanho decrescente primeiro,
    # menor índice de nó como desempate.
    ordem = np.lexsort((primeiro, -tamanhos))

    canonico = np.empty(quantos, dtype=np.int32)
    canonico[ordem] = np.arange(quantos, dtype=np.int32)
    rotulos = canonico[bruto].astype(np.int32)
    del bruto, canonico, primeiro

    tamanhos_ordenados = tamanhos[ordem]
    validar_rotulos_canonicos(rotulos, tamanhos_ordenados)

    parcial = destino.with_name(f"{destino.name}.parcial")
    with parcial.open("wb") as arquivo:
        np.save(arquivo, rotulos, allow_pickle=False)
    parcial.replace(destino)

    gigante = int(tamanhos_ordenados[0]) if quantos else 0
    resultado = Componentes(
        caminho=destino,
        nos=grafo.nos,
        quantos=quantos,
        maiores=tuple(int(t) for t in tamanhos_ordenados[:MAIORES_REPORTADOS]),
        gigante=gigante,
        fora_do_gigante=grafo.nos - gigante,
        isolados_no_csr=int((tamanhos_ordenados == 1).sum()),
        bytes_dos_componentes=destino.stat().st_size,
    )
    logger.info(
        "componentes conexos calculados",
        extra={
            "competencia": alvo,
            "uf_alvo": config.uf_alvo,
            "nos": resultado.nos,
            "componentes": resultado.quantos,
            "gigante": resultado.gigante,
            "fora_do_gigante": resultado.fora_do_gigante,
            "maiores": list(resultado.maiores),
            "isolados_no_csr": resultado.isolados_no_csr,
            "bytes_componentes": resultado.bytes_dos_componentes,
        },
    )
    return resultado
