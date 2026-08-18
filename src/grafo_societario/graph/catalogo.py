"""O catálogo de nós, lido sem Parquet: só NumPy e a biblioteca padrão.

`nos.parquet` guarda os metadados de cada nó, e ler Parquet exige pyarrow ou
DuckDB. Nenhum dos dois entra no caminho de resposta: pyarrow instalado ocupa 90 a
120 MB contra um teto de 300 MB de imagem, e traz um modelo de memória cujo pior
caso depende do tamanho do grupo de linhas e da RAM do contêiner — dois números
que só existem na Fase 8.

O catálogo é a mesma informação em `.npy` mais um blob de nomes comprimido em
blocos. **Pior caso de memória conhecido vence pior caso desconhecido** quando o
teto de RAM é a incógnita.

## Por que não é "escrever o próprio formato"

Não há esquema, não há evolução, não há tipos aninhados: são arrays de largura
fixa mapeados por `mmap` e um blob indexado por deslocamento. O que se lê de uma
posição é o que foi escrito nela.

Medido: os nomes em blocos de 64 KiB com `zlib` ocupam **63,6 MB**, contra 67,0 MB
do ZSTD do Parquet na mesma coluna. Uma consulta descomprime um bloco por nome, e
nenhum nome atravessa fronteira de bloco — o que faz a leitura ser um `slice`
depois de uma descompressão.

## O identificador não está aqui

Ele custava 44,4 MB e nenhum endpoint o consome: o identificador público de uma
pessoa jurídica é o **CNPJ**, e o hash é `sha256("pessoa_juridica|" + cnpj_basico)`
— derivável em uma linha por quem precisar. De pessoa física ele foi removido do
artefato publicável por ser reversível com a fonte aberta.

## Arrays paralelos são a falha recorrente deste projeto

É a terceira vez que a corretude depende de dois arrays estarem na mesma ordem:
`indices` com `qualificacoes` na serialização, `nos` com `identificadores` no
artefato, e agora `nome_offsets` com o blob. Nas três, desalinhar **não muda
contagem nenhuma** — muda só o significado de cada posição, e a resposta sai
plausível e errada.

Por isso cada par tem guarda posicional própria, e a deste é a ida e volta: todo
nome lido de volta tem de ser igual ao da origem, nos 5,02 milhões, não numa
amostra.
"""

from __future__ import annotations

import logging
import math
import zlib
from dataclasses import dataclass
from typing import Any, Final, Literal

import numpy as np

from grafo_societario.config import Config

logger = logging.getLogger(__name__)

MODO_DE_MAPEAMENTO: Final = "r"

TIPOS: Final = ("pessoa_juridica", "pessoa_fisica", "estrangeiro")

PESSOA_JURIDICA: Final = TIPOS[0]
"""O único tipo cujo nome é público. Os outros dois são nome de gente."""

CONFIANCAS: Final = ("exata", "estimada", "fraca", "nao_fundivel")
"""A ordem **é** a codificação: a posição na tupla é o valor gravado no int8.

Reordenar aqui reinterpreta todo artefato já gravado, sem nada falhar. Acrescentar
no fim é seguro; mexer no meio, não.
"""

ESTIMADA: Final = "estimada"
"""A única confiança que tem taxa de colisão. Ver `Catalogo.taxa_de_colisao_de`."""

SEM_REGIAO: Final = -1
"""Região fiscal de quem não tem CPF. `0` é dígito válido e não serve de ausência."""

REGIOES: Final = 10
"""Dígitos de região fiscal, de 0 a 9.

A tabela de taxas tem uma posição por dígito e **a posição é o dígito** — não a
ordem de aparição. São oitenta bytes; comprimi-la para os dígitos presentes
economizaria nada e faria a região 0 virar a região 1 no mês em que um dígito
não aparecesse."""


def procurar(
    ordenado: np.ndarray[Any, np.dtype[Any]],
    valor: int,
    lado: Literal["left", "right"] = "left",
) -> int:
    """Busca binária com o alvo **no tipo do array**, e isso não é detalhe.

    `np.searchsorted` com um `int` do Python contra um array int32 promove o array
    inteiro a int64 a cada chamada. Medido sobre os 20 MB de `cnpj_ordenado`:
    **8.289 µs contra 8,0 µs** — mil vezes mais lento, sem nada falhar e com o
    resultado certo.

    É a forma de defeito que só aparece medindo o endpoint: isolado, "busca
    binária" parece barata por definição.
    """
    return int(np.searchsorted(ordenado, ordenado.dtype.type(valor), side=lado))


ARQUIVOS: Final = (
    "cnpj_ordenado.npy",
    "no_por_cnpj.npy",
    "atributos.npy",
    "regiao_fiscal.npy",
    "taxa_por_regiao.npy",
    "nome_offsets.npy",
    "bloco_inicio.npy",
    "bloco_byte.npy",
    "nomes.bin",
)


class ErroDeCatalogo(RuntimeError):
    """Falha ao abrir ou consultar o catálogo de nós."""


class ArtefatoAusenteError(ErroDeCatalogo):
    """Falta um dos arquivos do catálogo."""


class ArtefatosIncompativeisError(ErroDeCatalogo):
    """Os arrays do catálogo não descrevem o mesmo conjunto de nós."""


class NoForaDaFaixaError(ErroDeCatalogo):
    """O índice pedido não endereça nó nenhum."""


@dataclass(frozen=True)
class No:
    """O que a API precisa dizer sobre um nó.

    `identificador` não está aqui de propósito: para pessoa jurídica o público é o
    CNPJ, e para pessoa física não existe identificador público — ver o topo do
    módulo.
    """

    indice: int
    tipo: str
    nome: str | None
    cnpj_basico: str | None
    regiao_fiscal: str | None
    confianca: str
    no_recorte: bool | None
    grau: int


@dataclass(frozen=True)
class Catalogo:
    """Os metadados dos nós, mapeados, com os nomes descomprimidos sob demanda."""

    cnpj_ordenado: np.ndarray[Any, np.dtype[np.int32]]
    no_por_cnpj: np.ndarray[Any, np.dtype[np.int32]]
    atributos: np.ndarray[Any, np.dtype[np.int8]]
    regiao_fiscal: np.ndarray[Any, np.dtype[np.int8]]
    taxa_por_regiao: np.ndarray[Any, np.dtype[np.float64]]
    """Dez valores, um por dígito de região. `NaN` onde não há pessoa física.

    **Derivado, não por nó.** A taxa é calculada por região na camada de
    identidade, então um array por nó guardaria 5,6 milhões de cópias de dez
    números — 45 MB para dizer dez coisas."""

    nome_offsets: np.ndarray[Any, np.dtype[np.int32]]
    bloco_inicio: np.ndarray[Any, np.dtype[np.int32]]
    bloco_byte: np.ndarray[Any, np.dtype[np.int64]]
    nomes: np.ndarray[Any, np.dtype[np.uint8]]
    competencia: str

    no_crescente: np.ndarray[Any, np.dtype[np.int32]]
    cnpj_do_no_crescente: np.ndarray[Any, np.dtype[np.int32]]
    """A volta do índice para o CNPJ, derivada na abertura e não gravada.

    São 40,2 MB de memória contra 42,6 MB de artefato, e é a mesma regra do
    commit 30: derive, não embarque."""

    @property
    def nos(self) -> int:
        return int(self.atributos.size)

    def indice_de(self, cnpj_basico: int) -> int | None:
        """O índice do nó de uma empresa, por busca binária. `None` se não é nó.

        Não confundir com existência: uma empresa pode estar no recorte e não ter
        vínculo nenhum, e aí ela não é nó. Quem responde existência é
        `existencia.npy`.
        """
        posicao = procurar(self.cnpj_ordenado, cnpj_basico)
        if posicao >= self.cnpj_ordenado.size:
            return None
        if int(self.cnpj_ordenado[posicao]) != cnpj_basico:
            return None
        return int(self.no_por_cnpj[posicao])

    def nome_de(self, indice: int) -> str | None:
        """O nome do nó, descomprimindo **um** bloco.

        Nenhum nome atravessa fronteira de bloco, então a leitura é uma
        descompressão seguida de um recorte. Nó sem nome — toda pessoa física no
        artefato publicável — tem faixa vazia e nem chega a abrir bloco.
        """
        self._validar(indice)
        inicio = int(self.nome_offsets[indice])
        fim = int(self.nome_offsets[indice + 1])
        if inicio == fim:
            return None
        bloco = procurar(self.bloco_inicio, inicio, "right") - 1
        comprimido = self.nomes[int(self.bloco_byte[bloco]) : int(self.bloco_byte[bloco + 1])]
        aberto = zlib.decompress(comprimido.tobytes())
        base = int(self.bloco_inicio[bloco])
        return aberto[inicio - base : fim - base].decode("utf-8")

    def _validar(self, indice: int) -> None:
        if not 0 <= indice < self.nos:
            raise NoForaDaFaixaError(
                f"O nó {indice:,} está fora da faixa 0..{self.nos - 1:,}. Índice negativo é "
                "válido para o NumPy e devolveria a faixa errada em silêncio."
            )

    def cnpj_basico_de(self, indice: int) -> str | None:
        """O `cnpj_basico` do nó, com o zero à esquerda recomposto.

        Só pessoa jurídica tem. A volta do índice para o CNPJ é derivada **uma vez
        na abertura**, e não a cada consulta: um array denso de `cnpj_basico` por
        nó custaria 42,6 MB no artefato para guardar o que já está aqui em outra
        ordem, e refazer a ordenação por consulta trocaria 42,6 MB de disco por
        `O(n log n)` no caminho de resposta. Derivar na partida paga uma vez.
        """
        self._validar(indice)
        posicao = procurar(self.no_crescente, indice)
        if posicao >= self.no_crescente.size or int(self.no_crescente[posicao]) != indice:
            return None
        return f"{int(self.cnpj_do_no_crescente[posicao]):08d}"

    def tipo_de(self, indice: int) -> str:
        self._validar(indice)
        return TIPOS[int(self.atributos[indice]) & 0b11]

    def confianca_de(self, indice: int) -> str:
        self._validar(indice)
        return CONFIANCAS[(int(self.atributos[indice]) >> 2) & 0b11]

    def no_recorte_de(self, indice: int) -> bool | None:
        self._validar(indice)
        marca = (int(self.atributos[indice]) >> 4) & 0b11
        return None if marca == 0 else marca == 2

    def regiao_de(self, indice: int) -> str | None:
        self._validar(indice)
        digito = int(self.regiao_fiscal[indice])
        return None if digito == SEM_REGIAO else str(digito)

    def taxa_de_colisao_de(self, indice: int) -> float | None:
        """Probabilidade de esta identidade ser duas pessoas, ou `None`.

        **Nulo não é dado faltando: é a grandeza não se aplicar.** A taxa mede
        fusão por máscara de CPF, e só pessoa física é fundida assim. Pessoa
        jurídica é identificada exatamente pelo CNPJ; estrangeiro não tem
        documento nenhum; e sócio **sem nome** é `nao_fundivel` — cada registro
        dele vira um nó próprio, e não há fusão a medir. Este último é o caso
        traiçoeiro: ele **tem** região fiscal, e derivar a taxa só da região
        daria um número aos 370 nós em que ela não significa nada.

        Zero seria pior que nulo nos três: afirmaria colisão impossível.

        O valor é o mesmo para toda identidade da mesma região, porque é assim
        que ele é calculado — daí a tabela de dez posições em vez de um array por
        nó.
        """
        self._validar(indice)
        if self.confianca_de(indice) != ESTIMADA:
            return None
        digito = int(self.regiao_fiscal[indice])
        if digito == SEM_REGIAO:
            return None
        valor = float(self.taxa_por_regiao[digito])
        return None if math.isnan(valor) else valor


def abrir_catalogo(config: Config, competencia: str | None = None) -> Catalogo:
    """Mapeia o catálogo e confere que os arrays descrevem o mesmo conjunto.

    As conferências são de tamanho, e nenhuma varre array: mapear tem de continuar
    barato. O que elas pegam é o catálogo remendado — arrays de execuções
    diferentes —, que responderia normalmente apontando para o nó errado.
    """
    alvo = competencia or config.competencia
    origem = config.data_dir / "grafo" / alvo
    faltando = [nome for nome in ARQUIVOS if not (origem / nome).exists()]
    if faltando:
        raise ArtefatoAusenteError(
            f"Faltam arquivos do catálogo em {origem}: {', '.join(faltando)}. Eles são "
            "produzidos na construção, e os oito precisam vir da mesma execução."
        )

    def mapear(nome: str) -> Any:
        return np.load(origem / nome, mmap_mode=MODO_DE_MAPEAMENTO)

    no_por_cnpj = mapear("no_por_cnpj.npy")
    cnpj_ordenado = mapear("cnpj_ordenado.npy")
    # A ordenação acontece uma vez, na partida. `stable` para o resultado não
    # depender da versão do NumPy: dois nós nunca empatam aqui, mas afirmar isso
    # sem garantir custaria o mesmo que garantir.
    ordem = np.argsort(no_por_cnpj, kind="stable")
    no_crescente = no_por_cnpj[ordem]
    cnpj_do_no_crescente = cnpj_ordenado[ordem]
    # Os mapeamentos abrem em modo "r" e já recusam escrita; estes dois nascem da
    # ordenação, em memória comum, e sairiam graváveis. O catálogo é aberto uma vez
    # e lido por várias threads ao mesmo tempo — o threadpool para onde o uvicorn
    # manda endpoint síncrono —, e array gravável compartilhado entre threads é a
    # combinação que não pode existir aqui. Nada escreve neles hoje, mas isso é
    # convenção, e a guarda custa duas linhas.
    no_crescente.flags.writeable = False
    cnpj_do_no_crescente.flags.writeable = False

    catalogo = Catalogo(
        cnpj_ordenado=cnpj_ordenado,
        no_crescente=no_crescente,
        cnpj_do_no_crescente=cnpj_do_no_crescente,
        no_por_cnpj=no_por_cnpj,
        atributos=mapear("atributos.npy"),
        regiao_fiscal=mapear("regiao_fiscal.npy"),
        taxa_por_regiao=mapear("taxa_por_regiao.npy"),
        nome_offsets=mapear("nome_offsets.npy"),
        bloco_inicio=mapear("bloco_inicio.npy"),
        bloco_byte=mapear("bloco_byte.npy"),
        nomes=np.memmap(origem / "nomes.bin", dtype=np.uint8, mode="r"),
        competencia=alvo,
    )
    _conferir(catalogo)
    logger.info(
        "catálogo mapeado",
        extra={"competencia": alvo, "nos": catalogo.nos, "empresas": catalogo.cnpj_ordenado.size},
    )
    return catalogo


def _conferir(catalogo: Catalogo) -> None:
    if catalogo.cnpj_ordenado.size != catalogo.no_por_cnpj.size:
        raise ArtefatosIncompativeisError(
            f"cnpj_ordenado tem {catalogo.cnpj_ordenado.size:,} entradas e no_por_cnpj tem "
            f"{catalogo.no_por_cnpj.size:,}. São arrays paralelos: desalinhados, o CNPJ de uma "
            "empresa devolve o nó de outra."
        )
    if catalogo.regiao_fiscal.size != catalogo.nos:
        raise ArtefatosIncompativeisError(
            f"atributos tem {catalogo.nos:,} nós e regiao_fiscal tem "
            f"{catalogo.regiao_fiscal.size:,}."
        )
    if catalogo.taxa_por_regiao.size != REGIOES:
        raise ArtefatosIncompativeisError(
            f"taxa_por_regiao tem {catalogo.taxa_por_regiao.size} posições e precisa de "
            f"{REGIOES}, uma por dígito de região fiscal. A posição é o dígito, e uma tabela "
            "mais curta faria a região 0 ser lida na posição de outra."
        )
    if catalogo.nome_offsets.size != catalogo.nos + 1:
        raise ArtefatosIncompativeisError(
            f"nome_offsets precisa ter um a mais que os nós: {catalogo.nome_offsets.size:,} "
            f"contra {catalogo.nos + 1:,}. É o array de fronteiras, e a última fecha o último "
            "nome."
        )
    if catalogo.bloco_byte.size != catalogo.bloco_inicio.size:
        raise ArtefatosIncompativeisError(
            f"bloco_inicio tem {catalogo.bloco_inicio.size:,} entradas e bloco_byte tem "
            f"{catalogo.bloco_byte.size:,}. Também são paralelos."
        )
    if catalogo.bloco_byte.size and int(catalogo.bloco_byte[-1]) != catalogo.nomes.size:
        raise ArtefatosIncompativeisError(
            f"o último bloco termina em {int(catalogo.bloco_byte[-1]):,} e o blob tem "
            f"{catalogo.nomes.size:,} bytes. O blob e o índice de blocos vieram de execuções "
            "diferentes."
        )
