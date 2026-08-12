"""Leitura do grafo em CSR, e por que ele é mapeado em vez de carregado.

## O motivo do `mmap` não é mais o que estava escrito

O ADR-002 justifica o CSR com `mmap` dizendo que o sistema pagina sob demanda e
que carregar o grafo inteiro seria inviável. Isso foi escrito quando o plano
supunha um grafo maior. **A medição desmentiu a premissa:** os três arrays somam
123,5 MiB, cabem em memória com folga larga, e "não caberia" deixou de ser
verdade no recorte de uma UF.

A escolha continua certa, por três motivos que sobrevivem à medição:

1. **Partida sem custo.** Abrir é mapear, não desserializar. O processo da API
   responde a primeira requisição sem ter lido 123,5 MiB de disco, e um free tier
   que hiberna e reacorda paga esse custo toda vez.
2. **Page cache compartilhado.** Dois processos do mesmo artefato dividem as
   mesmas páginas físicas. Com `np.load` sem `mmap_mode`, cada um paga a sua
   cópia inteira.
3. **Artefato imutável.** Ele não muda entre competências, é somente leitura, e
   páginas limpas o sistema descarta e relê sem escrever swap. Quem faz essa
   gestão melhor do que nós é o sistema operacional.

O texto do ADR-002 precisa dizer isso: a premissa caiu, a conclusão continua de
pé por outro fundamento. Está registrado como pendência do Commit 46.

## `mmap` não é "nunca carrega", é "carrega sob demanda"

A memória residente cresce conforme as páginas são tocadas. Uma travessia com
acesso aleatório sobre 66 MiB de `indices` acaba tocando quase tudo, e o número
honesto é o de **depois** dos acessos, não o de abrir. Ver
`tests/test_csr.py`, que mede os dois momentos.

## A fatia é vista, não cópia — e é fácil deixar de ser

`vizinhos` devolve uma fatia do mapeamento, que compartilha memória com ele: não
há alocação nem cópia, e por isso a leitura de um vizinho custa uma página e não
o arquivo. Mas **basta uma operação para materializar**: `list(v)`, `np.sort(v)`,
`v + 0`, `v.copy()`, ou qualquer aritmética devolvem array novo em memória
comum.

Quem consumir isto na travessia precisa saber disso, porque o sintoma de perder a
vista não é erro nenhum — é o mesmo resultado, com o `mmap` virando decoração
cara. Há teste afirmando o compartilhamento de memória, e não apenas que os
valores estão certos: valores certos uma cópia também tem.

## Este módulo não conhece o motor de ETL

`build.py` importa DuckDB; aqui não se importa nada além de NumPy e da
configuração. É o que permite a imagem de serving da Fase 8 não carregar o motor
que construiu o artefato, e o que mantém o caminho de resposta com uma superfície
de dependência que cabe na cabeça.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any, Final

import numpy as np

from grafo_societario.config import Config

logger = logging.getLogger(__name__)

MODO_DE_MAPEAMENTO: Final = "r"
"""Somente leitura. O artefato é imutável entre competências, e abrir para
escrita transformaria um defeito de código em corrupção do arquivo publicado."""

ARQUIVOS: Final = ("indptr.npy", "indices.npy", "qualificacoes.npy")


class ErroDeCsr(RuntimeError):
    """Falha ao abrir ou consultar o grafo serializado."""


class ArtefatoAusenteError(ErroDeCsr):
    """Falta um dos arrays do grafo."""


class ArtefatosIncompativeisError(ErroDeCsr):
    """Os três arrays não descrevem o mesmo grafo."""


class NoForaDaFaixaError(ErroDeCsr):
    """O índice pedido não endereça nó nenhum."""


@dataclass(frozen=True)
class Grafo:
    """O grafo mapeado, e as perguntas que se responde sobre ele sem varrer nada.

    Os três arrays são `numpy.memmap` abertos em modo leitura. Nenhum método aqui
    materializa linha: todos devolvem fatia, que é vista do mapeamento.
    """

    indptr: np.ndarray[Any, np.dtype[np.int32]]
    indices: np.ndarray[Any, np.dtype[np.int32]]
    qualificacoes: np.ndarray[Any, np.dtype[np.int8]]
    competencia: str

    @property
    def nos(self) -> int:
        return int(self.indptr.size) - 1

    @property
    def posicoes(self) -> int:
        """Posições em `indices`. Cada aresta ocupa duas: uma por extremo."""
        return int(self.indices.size)

    def _faixa(self, no: int) -> tuple[int, int]:
        """Onde a linha do nó começa e termina.

        A conferência de faixa não é zelo: um índice negativo é válido para o
        NumPy e **não levanta nada**. `vizinhos(-1)` leria `indptr[-1]` e
        `indptr[0]`, produziria uma fatia vazia, e responderia "este nó não tem
        vizinho" — plausível, silencioso e falso, que é o modo de falha que este
        projeto trata como o pior.
        """
        if not 0 <= no < self.nos:
            raise NoForaDaFaixaError(
                f"O nó {no:,} está fora da faixa 0..{self.nos - 1:,}. Índice negativo é válido "
                "para o NumPy e devolveria fatia vazia, que se lê como 'sem vizinhos' em vez de "
                "como erro."
            )
        return int(self.indptr[no]), int(self.indptr[no + 1])

    def grau(self, no: int) -> int:
        """Quantos vizinhos o nó tem, lendo dois inteiros e nada mais.

        **É grau dentro do recorte.** Só ingerimos sócios de empresas cuja matriz
        está na UF alvo, então quem participa de 3 empresas em SP e 40 no Rio
        aparece aqui com 3. O número é piso, nunca total — a mesma ressalva que
        `vinculos_no_recorte` carrega desde a camada de identidade.
        """
        inicio, fim = self._faixa(no)
        return fim - inicio

    def vizinhos(self, no: int) -> np.ndarray[Any, np.dtype[np.int32]]:
        """Os vizinhos do nó, como **vista** do mapeamento.

        Não há cópia nem alocação: a fatia compartilha memória com o arquivo
        mapeado, e ler um vizinho toca uma página em vez do arquivo. Materializar
        (`list`, `np.sort`, aritmética) devolve array novo e desfaz isso sem
        sintoma nenhum — o resultado continua certo e o `mmap` vira custo morto.

        O resultado vem **ordenado**, porque a serialização garante essa ordem
        dentro de cada linha. É o que permite `sao_vizinhos` em `O(log grau)`.
        """
        inicio, fim = self._faixa(no)
        return self.indices[inicio:fim]

    def qualificacoes_de(self, no: int) -> np.ndarray[Any, np.dtype[np.int8]]:
        """As qualificações dos vínculos do nó, alinhadas posição a posição com
        `vizinhos`. Também vista, pela mesma razão."""
        inicio, fim = self._faixa(no)
        return self.qualificacoes[inicio:fim]

    def sao_vizinhos(self, origem: int, destino: int) -> bool:
        """Adjacência por busca binária, em `O(log grau)`.

        É o que a ordenação dentro da linha comprou. Com grau máximo de 3.728, é
        a diferença entre doze comparações e três mil e setecentas — e a travessia
        da Fase 5 faz essa pergunta milhões de vezes.

        A busca é feita pelo lado de **menor grau**. O grafo é não direcionado, e
        as duas perguntas têm a mesma resposta, mas não o mesmo custo: procurar um
        hub na linha de um nó de grau 1 é uma comparação; o contrário são doze.
        """
        if self.grau(origem) > self.grau(destino):
            origem, destino = destino, origem
        linha = self.vizinhos(origem)
        posicao = int(np.searchsorted(linha, destino))
        return posicao < linha.size and int(linha[posicao]) == destino


def abrir_grafo(config: Config, competencia: str | None = None) -> Grafo:
    """Mapeia os três arrays e confere que eles descrevem o mesmo grafo.

    As conferências são todas de tamanho e tipo, e nenhuma varre array: mapear
    tem de continuar sendo barato, senão o motivo de mapear desaparece. O que elas
    pegam é o artefato remendado — `indices` de uma execução com `indptr` de
    outra, que é o acidente mais fácil de cometer com três arquivos soltos e o
    mais difícil de perceber depois, porque a consulta responde normalmente e
    aponta para o nó errado.

    As conferências caras — simetria, ordem dentro da linha, atributo alinhado —
    são de quem grava, não de quem lê. Elas rodaram na serialização e não se
    repetem a cada partida da API.
    """
    alvo = competencia or config.competencia
    origem = config.data_dir / "grafo" / alvo
    faltando = [nome for nome in ARQUIVOS if not (origem / nome).exists()]
    if faltando:
        raise ArtefatoAusenteError(
            f"Faltam arrays do grafo em {origem}: {', '.join(faltando)}. Eles são produzidos "
            "pela serialização em CSR, e os três precisam vir da mesma execução."
        )

    indptr = np.load(origem / "indptr.npy", mmap_mode=MODO_DE_MAPEAMENTO)
    indices = np.load(origem / "indices.npy", mmap_mode=MODO_DE_MAPEAMENTO)
    qualificacoes = np.load(origem / "qualificacoes.npy", mmap_mode=MODO_DE_MAPEAMENTO)

    if indptr.size < 1:
        raise ArtefatosIncompativeisError(
            "indptr precisa ter ao menos uma entrada: ele tem uma por nó, mais uma. Vazio, "
            "não descreve grafo nenhum."
        )
    declarado = int(indptr[-1])
    if declarado != indices.size:
        raise ArtefatosIncompativeisError(
            f"indptr termina em {declarado:,} e indices tem {indices.size:,} posições. Os dois "
            "vieram de execuções diferentes, e a consulta responderia normalmente apontando "
            "para o nó errado."
        )
    if qualificacoes.size != indices.size:
        raise ArtefatosIncompativeisError(
            f"indices tem {indices.size:,} posições e qualificacoes tem "
            f"{qualificacoes.size:,}. O atributo é paralelo a indices, e desalinhado ele troca "
            "o significado de cada posição sem mudar contagem nenhuma."
        )

    grafo = Grafo(
        indptr=indptr,
        indices=indices,
        qualificacoes=qualificacoes,
        competencia=alvo,
    )
    logger.info(
        "grafo mapeado",
        extra={
            "competencia": alvo,
            "nos": grafo.nos,
            "posicoes": grafo.posicoes,
            "modo": MODO_DE_MAPEAMENTO,
        },
    )
    return grafo
