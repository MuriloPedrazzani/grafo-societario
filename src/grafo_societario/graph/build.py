"""Os nós do grafo, e a fronteira entre estar no grafo e existir.

## Nem toda empresa do recorte é um nó do grafo

Das 19.770.618 empresas do recorte de SP, **14.791.390 não têm nenhum vínculo** —
74,8%. São, quase todas, empresário individual: o dono está dentro da razão social
e o projeto recusa extraí-lo de lá, então nenhum vínculo é registrado. Ver a
decisão e o custo em `transform.silver`.

Um nó sem vínculo nenhum não pode estar em caminho societário nenhum. Carregá-lo
no CSR custa uma entrada de `indptr` e uma linha de metadados por nada, e é o que
estoura o orçamento de 500 MB do artefato — o gargalo do deploy são os metadados
dos nós, não os arrays do grafo.

## O invariante exato, porque o óbvio é falso

**Todo nó de `nos.parquet` tinha ao menos um vínculo em `arestas.parquet`.** Essa é
a afirmação que vale, e ela não é a mesma que "todo nó do CSR tem ao menos um
vizinho" — que **é falsa**.

A serialização descarta laço, e 30 nós tinham como único vínculo um laço: uma
empresa registrada como sócia de si mesma e de mais ninguém. Eles entraram em
`nos.parquet` porque tinham vínculo, e saíram do CSR com **grau 0** porque o
vínculo que tinham não liga a lugar nenhum.

Trinta em 10.658.250 é pouco, e é justamente por isso que a versão errada do
invariante sobreviveria: ela passa em qualquer amostra. Quem escrever guarda,
travessia ou métrica sobre "todo nó tem vizinho" vai acertar 99,9997% das vezes e
errar sem sintoma no resto. O número muda a cada competência; o mecanismo, não.

## Mas "não tem vínculo" e "não existe" são respostas diferentes

Deixar os isolados de fora do CSR não pode transformar uma consulta sobre eles em
"empresa não encontrada". Isso seria falso: a empresa existe, está no recorte, e a
resposta certa é que ela não tem vínculo societário registrado.

Daí a separação em dois artefatos:

- **`nos.parquet`** — os 10.658.250 nós com pelo menos um vínculo e os atributos de
  cada um, ordenados por identificador. É o dicionário reverso do grafo, e o índice
  de um nó é a posição da linha.
- **`existencia.npy`** — os 19.770.618 `cnpj_basico` do recorte, como int32
  ordenado. Responde existência por busca binária, **exatamente**: sem falso
  positivo, ao contrário de um filtro probabilístico, e sem carregar metadado de
  quem não tem vínculo.

## Nome de pessoa física não entra no artefato publicado

`nos.parquet` vai para GitHub Release e para imagem Docker, e 5,6 milhões dos seus
nós são gente. Pseudonimizar na resposta da API não desfaria nada — é o mesmo
argumento que moveu a supressão de CPF para a transformação, aplicado ao campo
vizinho: o que entra no artefato já saiu.

`EXPOR_PF` decide na geração. Falso, que é o padrão e o modo do artefato publicado,
deixa o nome nulo para pessoa física e para estrangeiro. Verdadeiro é para quem
roda o pipeline localmente sobre os dados originais — o código é aberto para isso.

Razão social de pessoa jurídica permanece nos dois modos: é o nome legal do
negócio, sai em nota fiscal e no cartão CNPJ, e a distinção já está registrada em
`transform.silver`.

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

## As arestas conservam o vínculo, e não a topologia

`arestas.parquet` tem **uma linha por vínculo**, não por par de nós. As duas coisas
quase coincidem — 8.699.764 vínculos para 8.699.585 pares distintos — mas a
diferença é onde a qualificação mora: 56 pares aparecem mais de uma vez, somando
235 vínculos, porque a mesma pessoa pode ser sócia e administradora da mesma
empresa em dois registros.

Colapsar aqui jogaria fora o atributo antes de alguém decidir o que fazer com ele.
Quem colapsa é a serialização em CSR, que é onde o par vira posição de array e a
repetição passa a custar. Esta camada conserva o que o silver entregou, e o que
ela conserva é conferido: entram 8.699.764 vínculos e saem 8.699.764 arestas, ou
o pipeline para.

## O que esta camada mede para a seguinte decidir

Três números vão no log de toda execução, **inclusive quando são zero**, porque o
zero de hoje não é o de todo mês:

- **laços** (empresa sócia de si mesma): 9.049. São legais em CSR e inúteis para
  caminho — ninguém chega a lugar nenhum por eles — e inflam o grau. Saem na
  serialização, contados.
- **pares paralelos**: 56, somando 179 vínculos excedentes. Colapsam em uma aresta.
- **pares com qualificação divergente**: 0. É o número que decide se o colapso
  perde informação. Enquanto for zero, os 179 excedentes são repetição exata e o
  colapso não escolhe nada. Quando deixar de ser, a regra é `min` do código —
  determinística e total, porque a largura do código é fixa — e o contador é o que
  faz a escolha aparecer em vez de ser absorvida.

Contar sempre, inclusive no zero, é o mesmo padrão do colapso de matriz duplicada
do recorte: um número que só aparece quando incomoda é um número em que ninguém
repara quando passa a incomodar.
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
from grafo_societario.transform.identity import (
    EXPRESSAO_DO_IDENTIFICADOR,
    TIPOS,
    consulta_de_socios_identificados,
    expressao_do_no_de_empresa,
    instalar_identificador,
)

logger = logging.getLogger(__name__)

COLUNAS_NOS: Final = (
    "identificador",
    "tipo",
    "nome",
    "cnpj_basico",
    "cpf_mascarado",
    "regiao_fiscal",
    "pais",
    "no_recorte",
    "confianca",
    "taxa_de_colisao",
)
"""O dicionário reverso: da posição da linha para o que o nó é.

**Não há coluna `indice`.** O índice de um nó é a posição da linha, porque o
arquivo é gravado ordenado por `identificador` e lido inteiro. Gravá-lo custava
27,8 MiB para repetir o número da linha em que o número já estava — não é dívida
a pagar depois, é dado que não precisa existir.

A validação de densidade continua, agora sobre a posição: ela confere que a
ordenação é estrita e sem repetição, que é o que garante que a linha `k` é o nó
`k`.
"""

TIPO_DE_EMPRESA: Final = TIPOS["1"]

POSICAO_DA_REGIAO: Final = 9
"""Onde a região fiscal aparece na máscara `***DDDDDD**`.

É o nono caractere, e o nono dígito do CPF: o mascaramento da Receita deixa
justamente esse à mostra. Num recorte de SP ele é `8` em 86,65% dos sócios, o que
reduz o espaço efetivo da máscara de 10⁶ para 132.705 e faz a taxa de colisão
variar vinte vezes entre regiões. Ver `transform.identity`.
"""

COLUNAS_ARESTAS: Final = ("no_empresa", "no_socio", "qualificacao_socio")
"""Os dois extremos, como índice denso, e o que o vínculo era.

Os nomes preservam a assimetria. O grafo é **não direcionado** — sócio e empresa
alcançam um ao outro, e é isso que a Fase 5 percorre — mas a relação não é
simétrica: "A é sócia de B" não é a mesma frase que "B é sócia de A", e num
produto de compliance é a frase que interessa. Chamar as colunas de `origem` e
`destino` inventaria uma direção que a travessia não tem; chamá-las pelo papel
guarda o que a fonte disse, e a serialização simetriza por cima disso.

`qualificacao_socio` é código, e código é texto — inclusive aqui, onde os dois
vizinhos são inteiros. A conversão para inteiro é decisão da serialização, que é
quem tem um array para preencher, e ela precisa vir com a asserção de que o valor
cabe no tipo escolhido.
"""

TIPOS_DE_PESSOA_FISICA: Final = (TIPOS["2"], TIPOS["3"])
"""Os dois tipos de nó que são gente: pessoa física e estrangeiro.

Estrangeiro entra aqui porque é pessoa, e não porque tem documento — ele não tem
nenhum. A regra é sobre quem o nó é, não sobre o que se sabe dele.
"""

SENTINELA_DE_QUALIFICACAO: Final = -1
"""O que ocupa a posição quando o vínculo não tem qualificação.

Um array de int8 não tem nulo, e o código `0` está ocupado: ele existe na fonte e
significa "não informada", que é diferente de ausente. `-1` está livre porque
código da Receita não é negativo, e escolher um valor impossível em vez de um
improvável é o que impede a ausência de virar um código de verdade.

No recorte de SP de 2026-06 ele não aparece nenhuma vez — os 8.699.764 vínculos
têm qualificação. A sentinela existe porque "zero nesta competência" não é
"zero sempre", e descobrir isso com um `IndexError` na Fase 6 seria tarde.
"""


def _nome_publicavel(expor_pf: bool) -> str:
    """Expressão do nome do nó, conforme o artefato vá ser publicado ou não.

    **Nome de pessoa física não entra no artefato publicado.** `nos.parquet` vai
    para GitHub Release e para imagem Docker, e são 5,6 milhões de nomes de gente.
    Pseudonimizar na resposta da API não desfaria nada — é o mesmo argumento que
    moveu a supressão de CPF para a transformação, aplicado ao campo vizinho.

    Quem roda local com `EXPOR_PF=true` gera o seu artefato com os nomes: o código
    é aberto e os dados são os originais da Receita. O que não acontece é o
    artefato **publicado** carregá-los.

    Razão social de pessoa jurídica permanece nos dois casos. Ela é o nome legal
    do negócio, sai em nota fiscal e no cartão CNPJ — a distinção é a mesma já
    registrada em `transform.silver`, e não mudou.
    """
    if expor_pf:
        return "nome"
    pessoas = ", ".join(f"'{tipo}'" for tipo in TIPOS_DE_PESSOA_FISICA)
    return f"CASE WHEN tipo IN ({pessoas}) THEN NULL ELSE nome END"


def _mascara_publicavel(expor_pf: bool) -> str:
    """Expressão do CPF mascarado, conforme o artefato vá ser publicado ou não.

    **A máscara é chave de junção de volta à fonte.** Com `***123456**` e o fato
    de o nó ser sócio da empresa X, recupera-se o nome no arquivo `Socios` da
    própria Receita — que é público. Nó pseudonimizado que carrega a chave de
    busca não está pseudonimizado; é a mesma falha de categoria do nome, num campo
    menor e mais fácil de justificar.

    Ela também não é necessária ali. O que a API precisa dizer sobre a confiança
    da identidade é a taxa de colisão, e essa depende apenas do dígito de região
    fiscal — que vai em coluna própria. Um dígito no lugar de seis: a
    funcionalidade fica, a identificabilidade sai.

    Com `EXPOR_PF=true`, em execução local sobre os dados originais, a máscara
    permanece: lá ela não é chave para nada que já não esteja aberto na mesma
    máquina.
    """
    return "cpf_mascarado" if expor_pf else "CAST(NULL AS VARCHAR)"


class ErroDeGrafo(RuntimeError):
    """Falha ao construir o grafo."""


class SilverAusenteError(ErroDeGrafo):
    """A construção foi pedida antes de a camada silver existir."""


class IndiceNaoDensoError(ErroDeGrafo):
    """O índice não cobre exatamente 0..N-1."""


class ExistenciaDesordenadaError(ErroDeGrafo):
    """O array de existência não está ordenado, e a busca binária mentiria."""


class NosAusentesError(ErroDeGrafo):
    """As arestas foram pedidas antes de os nós existirem."""


class ExtremoDesconhecidoError(ErroDeGrafo):
    """Uma aresta tem extremo que não é nó."""


class ArestaPerdidaError(ErroDeGrafo):
    """Entraram vínculos e saíram menos arestas."""


class IndiceForaDaFaixaError(ErroDeGrafo):
    """Uma aresta endereça posição que não existe no conjunto de nós."""


class ArestasAusentesError(ErroDeGrafo):
    """A serialização foi pedida antes de a lista de arestas existir."""


class QualificacaoNaoNumericaError(ErroDeGrafo):
    """Um código de qualificação não é número, e não cabe em array de inteiro."""


class QualificacaoForaDoTipoError(ErroDeGrafo):
    """Um código de qualificação não cabe no tipo escolhido para o array."""


class VizinhoDesordenadoError(ErroDeGrafo):
    """Os vizinhos de uma linha não estão em ordem estritamente crescente."""


class SimetriaQuebradaError(ErroDeGrafo):
    """O grafo não é simétrico, ou o atributo não acompanhou a simetrização."""


@dataclass(frozen=True)
class Nos:
    """O que a geração produziu."""

    caminho: Path
    caminho_da_existencia: Path

    nos: int
    """Nós com pelo menos um **vínculo**. São os que entram no CSR.

    Ter vínculo não é ter vizinho: 30 destes tinham como único vínculo um laço,
    que a serialização descarta, e ficam no CSR com grau 0. Ver o invariante no
    topo do módulo."""

    por_tipo: tuple[tuple[str, int], ...]

    existencia: int
    """Empresas do recorte cuja existência é respondível — todas, com ou sem vínculo."""

    isolados: int
    """Empresas do recorte sem nenhum vínculo. Ficam fora do grafo e dentro da
    existência: consultá-las devolve "sem vínculo", nunca "não existe"."""

    bytes_dos_nos: int
    bytes_da_existencia: int

    expor_pf: bool
    """Se este artefato carrega nome de pessoa física. Falso é o modo publicável;
    verdadeiro só se justifica em execução local sobre os dados originais."""


@dataclass(frozen=True)
class Arestas:
    """O que a geração produziu, e o que a serialização precisa saber."""

    caminho: Path

    arestas: int
    """Uma linha por vínculo. Conservado a partir do silver, e conferido."""

    pares_distintos: int
    """Quantas arestas o CSR terá depois do colapso, laços ainda incluídos."""

    lacos: int
    """Vínculos de uma empresa consigo mesma. Saem na serialização, contados: são
    legais em CSR, não levam a lugar nenhum, e inflam o grau de quem os tem."""

    pares_paralelos: int
    """Pares que aparecem em mais de um vínculo. Colapsam em uma aresta."""

    pares_com_qualificacao_divergente: int
    """Dos paralelos, quantos discordam na qualificação — os únicos em que o
    colapso escolhe, em vez de apenas repetir. Enquanto for zero, nada se perde."""

    bytes_das_arestas: int


@dataclass(frozen=True)
class Csr:
    """O grafo em formato CSR, e o que precisou ser descartado para ele existir."""

    caminho_do_indptr: Path
    caminho_dos_indices: Path
    caminho_das_qualificacoes: Path

    nos: int

    arestas: int
    """Pares **não ordenados** distintos. É o número de arestas do grafo."""

    posicoes: int
    """`2 * arestas`. Cada aresta ocupa uma posição na linha de cada extremo."""

    lacos_descartados: int
    """Vínculos de uma empresa consigo mesma. Não levam a lugar nenhum e inflariam
    o grau de quem os tem, então saem — contados, nunca em silêncio."""

    pares_com_qualificacao_divergente: int
    """Arestas cujos vínculos discordam da qualificação, e onde `min` escolhe de
    fato. Fora desses, o colapso só repete o que já era igual."""

    pares_mutuos: int
    """A é sócia de B **e** B é sócia de A. Colapsar pelo par ordenado deixaria
    esses vizinhos repetidos dentro da mesma linha do CSR."""

    grau_maximo: int
    bytes_totais: int


def validar_qualificacao_cabe_em_int8(
    valores: np.ndarray[Any, np.dtype[np.integer[Any]]],
) -> None:
    """Recusa qualificação que não caiba no tipo do array paralelo.

    A largura de dois dígitos foi **medida nesta competência**, e o layout oficial
    não a declara — o mesmo PDF já errou três vezes sobre estes arquivos, e o
    código `36` nem existe na tabela de domínio que deveria contê-lo. Concluir daí
    que o código nunca passa de 99 seria transformar observação em garantia.

    Então a garantia é esta linha, e não o raciocínio: se um dia entrar código de
    três dígitos, a serialização para aqui em vez de gravar o resto da divisão por
    256 e servir "Sócio-Administrador" onde era outra coisa.
    """
    if not valores.size:
        return
    menor, maior = int(valores.min()), int(valores.max())
    limite = np.iinfo(np.int8)
    if menor < limite.min or maior > limite.max:
        raise QualificacaoForaDoTipoError(
            f"As qualificações vão de {menor} a {maior}, fora da faixa "
            f"{limite.min}..{limite.max} do int8. Estreitar assim mesmo grava o resto da "
            "divisão e devolve a qualificação de outro código, sem erro nenhum."
        )


def validar_indice_cabe_em_int32(
    valores: np.ndarray[Any, np.dtype[np.integer[Any]]],
) -> None:
    """Recusa `indptr` que não caiba no tipo escolhido para o array.

    O maior valor medido é 17.379.764 contra um teto de 2.147.483.647 — folga de
    123 vezes, e é essa folga que justifica int32 em vez de int64, economizando
    107 MiB sobre um orçamento de 500 MB. Justificativa medida vale para hoje; a
    asserção vale para o dia em que o recorte deixar de ser uma UF.

    Sem ela, o estreitamento é um `and` com `0xFFFFFFFF`: `indptr` volta a
    crescer do zero no meio do arquivo, e todo nó depois disso passa a apontar
    para os vizinhos de outro.
    """
    if not valores.size:
        return
    maior = int(valores.max())
    limite = np.iinfo(np.int32)
    if maior > limite.max:
        raise IndiceForaDaFaixaError(
            f"O maior valor de indptr é {maior:,}, acima do teto {limite.max:,} do int32. "
            "Estreitar assim mesmo faz o ponteiro dar a volta e apontar para os vizinhos de "
            "outro nó, sem erro."
        )


def validar_vizinhos_ordenados(
    indptr: np.ndarray[Any, np.dtype[np.int32]],
    indices: np.ndarray[Any, np.dtype[np.int32]],
) -> None:
    """Recusa CSR cujos vizinhos não estejam ordenados dentro de cada linha.

    É o invariante canônico do formato, e não é decoração: com a linha ordenada,
    perguntar se dois nós são vizinhos custa `O(log grau)` por busca binária, em
    vez de varrer a linha inteira. Num grafo cujo maior grau é 3.728, é a
    diferença entre onze comparações e três mil e setecentas — e a travessia da
    Fase 5 faz essa pergunta milhões de vezes.

    A ordenação estrita também é o que garante que **não há vizinho repetido**:
    aresta paralela sobrevivente apareceria aqui como igualdade, não como erro de
    contagem.

    Refazer a ordem depois custa reescrever o artefato inteiro, então ela é
    afirmada agora.
    """
    if indices.size < 2:
        return
    inicio_de_linha = np.zeros(indices.size, dtype=bool)
    inicio_de_linha[indptr[:-1][np.diff(indptr) > 0]] = True
    interior = ~inicio_de_linha[1:]
    if not bool(np.all(indices[1:][interior] > indices[:-1][interior])):
        raise VizinhoDesordenadoError(
            "Os vizinhos precisam ficar em ordem estritamente crescente dentro de cada linha. "
            "Fora de ordem, a busca binária de adjacência responde 'não são vizinhos' para "
            "quem é; repetido, o mesmo vizinho é visitado duas vezes e o grau mente."
        )


def validar_simetria(
    indptr: np.ndarray[Any, np.dtype[np.int32]],
    indices: np.ndarray[Any, np.dtype[np.int32]],
    qualificacoes: np.ndarray[Any, np.dtype[np.int8]],
) -> None:
    """Recusa grafo assimétrico, e atributo que não acompanhou a simetrização.

    Cada aresta ocupa **duas** posições: uma na linha de um extremo, outra na do
    outro. A qualificação tem de estar idêntica nas duas, e é aqui que mora o modo
    de falha mais silencioso de todo o formato: se o array de atributo
    dessincronizar de `indices`, **nenhuma contagem muda**. O tamanho continua
    certo, a topologia continua certa, `indptr` continua somando — só o
    significado de cada posição fica trocado, e a API passa a dizer que fulano é
    administrador de uma empresa de que ele é apenas sócio.

    A conferência é o espelho: para cada posição que diz "de `u` para `v`" tem de
    existir uma que diga "de `v` para `u`", e as duas têm de carregar a mesma
    qualificação. Comparar a matriz com a sua transposta é a única checagem que
    um deslocamento de atributo não sobrevive.
    """
    nos = int(indptr.size) - 1
    if not indices.size:
        return
    linhas = np.repeat(np.arange(nos, dtype=np.int64), np.diff(indptr))
    colunas = indices.astype(np.int64)
    direta = linhas * nos + colunas
    transposta = colunas * nos + linhas
    del linhas, colunas

    ordem = np.argsort(transposta, kind="stable")
    if not np.array_equal(transposta[ordem], direta):
        raise SimetriaQuebradaError(
            "O grafo precisa ser simétrico: toda aresta de u para v tem de ter a de v para u. "
            "Sem isso a travessia alcança um extremo a partir do outro e não o contrário, e o "
            "caminho existe ou não conforme a ponta pela qual se pergunta."
        )
    del direta, transposta

    if not np.array_equal(qualificacoes[ordem], qualificacoes):
        raise SimetriaQuebradaError(
            "A qualificação precisa ser idêntica nas duas posições da mesma aresta. Atributo "
            "dessincronizado de indices não muda contagem nenhuma — muda só o significado de "
            "cada posição, e a resposta sai plausível e errada."
        )


def validar_extremos_conhecidos(conexao: duckdb.DuckDBPyConnection, fonte: str) -> None:
    """Recusa aresta cujo extremo não é nó.

    Por construção isto é zero: todo `cnpj_basico` que aparece em `socios` entrou
    no conjunto de nós, e toda identidade de sócio também. É **por** ser zero que a
    guarda existe — o dia em que deixar de ser, o sintoma não seria uma exceção, e
    sim um índice nulo virando posição de array em algum ponto adiante.
    """
    medida = conexao.execute(
        f"SELECT count(*) FILTER (WHERE no_empresa IS NULL), "
        f"count(*) FILTER (WHERE no_socio IS NULL) FROM {fonte}"
    ).fetchone()
    sem_empresa, sem_socio = tuple(int(valor) for valor in medida) if medida else (1, 1)
    if sem_empresa or sem_socio:
        raise ExtremoDesconhecidoError(
            f"{sem_empresa:,} arestas não acharam a empresa e {sem_socio:,} não acharam o sócio "
            "no conjunto de nós. Extremo que não é nó vira índice nulo, e índice nulo endereça "
            "posição de array que não existe."
        )


def validar_arestas_conservadas(vinculos: int, arestas: int) -> None:
    """Recusa perda silenciosa de vínculo entre o silver e o grafo.

    É a mesma conferência que a tipagem de sócios faz, pela mesma razão e no
    degrau seguinte: aresta descartada em silêncio é caminho societário que deixa
    de existir sem ninguém saber que existia.
    """
    if vinculos != arestas:
        raise ArestaPerdidaError(
            f"Entraram {vinculos:,} vínculos e saíram {arestas:,} arestas. Alguma junção "
            "descartou vínculo, e vínculo descartado em silêncio é caminho societário que "
            "some sem deixar sintoma — o grafo continua íntegro, só não liga o que ligava."
        )


def validar_indice_na_faixa(conexao: duckdb.DuckDBPyConnection, fonte: str, nos: int) -> None:
    """Recusa índice que não endereça nó existente.

    O CSR indexa array pela posição, sem conferir nada em tempo de consulta. Um
    índice fora da faixa é, na melhor das hipóteses, erro de leitura fora do
    limite; na pior, e é a mais provável com `mmap`, **o nó errado devolvido sem
    erro nenhum**.
    """
    medida = conexao.execute(
        f"SELECT min(least(no_empresa, no_socio)), max(greatest(no_empresa, no_socio)) FROM {fonte}"
    ).fetchone()
    if not medida or medida[0] is None:
        return
    menor, maior = int(medida[0]), int(medida[1])
    if menor < 0 or maior >= nos:
        raise IndiceForaDaFaixaError(
            f"Os índices das arestas vão de {menor:,} a {maior:,}, fora da faixa 0..{nos - 1:,} "
            f"dos {nos:,} nós. Índice fora da faixa não falha na leitura por mmap: devolve o nó "
            "errado, sem erro."
        )


def validar_indice_denso(conexao: duckdb.DuckDBPyConnection, caminho: Path) -> None:
    """Recusa arquivo em que a posição da linha não seja um índice denso.

    Sem coluna de índice, o índice **é** a posição da linha, e isso só vale se o
    arquivo estiver ordenado por `identificador` sem repetição. Repetição faria
    dois nós disputarem a mesma posição, e desordem faria a linha `k` deixar de ser
    o nó `k` — nos dois casos o sintoma é caminho societário errado, não exceção.
    """
    medida = conexao.execute(
        f"""
        SELECT count(*), count(DISTINCT identificador),
               count(*) FILTER (WHERE identificador <= anterior)
        FROM (
          SELECT identificador, lag(identificador) OVER () AS anterior
          FROM read_parquet('{caminho.as_posix()}')
        )
        """
    ).fetchone()
    quantos, distintos, fora_de_ordem = (
        tuple(int(valor) for valor in medida) if medida else (0, 0, 1)
    )
    if distintos != quantos or fora_de_ordem:
        raise IndiceNaoDensoError(
            f"O arquivo precisa estar ordenado por identificador, sem repetição, para que a "
            f"linha k seja o nó k: são {quantos:,} linhas, {distintos:,} identificadores "
            f"distintos e {fora_de_ordem:,} fora de ordem. Índice que não é denso faz o CSR "
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

    Um nó entra se tiver pelo menos um vínculo, de qualquer um dos dois lados: a
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
            SELECT {expressao_do_no_de_empresa("e.cnpj_basico")} AS identificador,
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
              SELECT identificador, tipo, {_nome_publicavel(config.expor_pf)} AS nome,
                     cnpj_basico,
                     {_mascara_publicavel(config.expor_pf)} AS cpf_mascarado,
                     substr(cpf_mascarado, {POSICAO_DA_REGIAO}, 1) AS regiao_fiscal,
                     pais, no_recorte, confianca, taxa_de_colisao
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
        expor_pf=config.expor_pf,
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
            "expor_pf": config.expor_pf,
        },
    )
    return resultado


def gerar_arestas(config: Config, competencia: str | None = None) -> Arestas:
    """Liga cada vínculo do silver a um par de índices densos.

    **A aresta tem dois extremos, e os dois saem da mesma regra.** O nó da empresa
    vem de `expressao_do_no_de_empresa`, e o do sócio de
    `EXPRESSAO_DO_IDENTIFICADOR` — que usa aquela no ramo de pessoa jurídica. É
    isso que faz a empresa vista como titular do vínculo e a mesma empresa vista
    como sócia caírem no mesmo nó, em vez de em dois.

    **O índice vem do arquivo, não de um cálculo paralelo.** `nos.parquet` é lido e
    numerado pela ordem em que está gravado, que é a definição do índice no commit
    anterior. Recalculá-lo a partir do silver daria o mesmo número hoje e um número
    diferente no dia em que a geração de nós mudasse de critério — e o artefato
    publicado seria o outro.

    **Uma linha por vínculo, e não por par.** A qualificação é atributo do vínculo:
    a mesma pessoa é sócia e administradora da mesma empresa em dois registros.
    Colapsar aqui descartaria o atributo antes de a serialização decidir o que
    fazer com ele — e é lá, onde o par vira posição de array, que a repetição custa.
    """
    alvo = competencia or config.competencia
    socios = config.data_dir / "silver" / alvo / "socios.parquet"
    if not socios.exists():
        raise SilverAusenteError(
            f"Não há sócios tipados em {socios}. As arestas são os vínculos do silver, "
            "e não do bronze."
        )

    nos_parquet = config.data_dir / "grafo" / alvo / "nos.parquet"
    if not nos_parquet.exists():
        raise NosAusentesError(
            f"Não há nós em {nos_parquet}. O índice de uma aresta é a posição da linha nesse "
            "arquivo, então ele precisa existir antes — e ser o mesmo que vai ser publicado."
        )
    destino = nos_parquet.with_name("arestas.parquet")

    with abrir_conexao(config, config.data_dir / "duckdb-tmp") as conexao:
        instalar_identificador(conexao)
        conexao.execute("SET preserve_insertion_order=false")

        # O `ORDER BY` não é decorativo: com `preserve_insertion_order=false` o
        # motor não devolve as linhas na ordem em que as leu, e sem ele a numeração
        # sairia diferente da gravada — que é justamente o que define o índice.
        conexao.execute(
            f"""
            CREATE OR REPLACE TEMP TABLE no AS
            SELECT identificador,
                   CAST(row_number() OVER (ORDER BY identificador) - 1 AS INTEGER) AS indice
            FROM read_parquet('{nos_parquet.as_posix()}')
            """
        )
        medida = conexao.execute("SELECT count(*) FROM no").fetchone()
        nos = int(medida[0]) if medida else 0

        conexao.execute(
            f"""
            CREATE OR REPLACE TEMP TABLE vinculo AS
            SELECT {expressao_do_no_de_empresa("cnpj_basico")} AS empresa,
                   {EXPRESSAO_DO_IDENTIFICADOR} AS socio,
                   qualificacao_socio
            FROM ({consulta_de_socios_identificados(socios)})
            """
        )
        medida = conexao.execute("SELECT count(*) FROM vinculo").fetchone()
        vinculos = int(medida[0]) if medida else 0

        # LEFT, e não INNER, de propósito: com INNER um extremo desconhecido
        # sumiria como linha a menos, e a conferência de conservação diria "aresta
        # perdida" sem dizer de que lado. Com LEFT ele vira nulo e a guarda nomeia
        # o extremo.
        conexao.execute(
            """
            CREATE OR REPLACE TEMP TABLE aresta AS
            SELECT e.indice AS no_empresa, s.indice AS no_socio, v.qualificacao_socio
            FROM vinculo v
            LEFT JOIN no e ON e.identificador = v.empresa
            LEFT JOIN no s ON s.identificador = v.socio
            """
        )
        validar_extremos_conhecidos(conexao, "aresta")

        # A terceira coluna entra na ordenação porque as duas primeiras não bastam:
        # 56 pares aparecem em mais de um vínculo. Ela não torna a ordem total —
        # linhas que empatam nas três são idênticas, e linhas idênticas são
        # intercambiáveis, então os bytes saem iguais de qualquer maneira.
        parcial = destino.with_name(f"{destino.name}.parcial")
        conexao.execute(
            f"""
            COPY (
              SELECT no_empresa, no_socio, qualificacao_socio FROM aresta
              ORDER BY no_empresa, no_socio, qualificacao_socio
            ) TO '{parcial.as_posix()}' (FORMAT PARQUET, COMPRESSION ZSTD)
            """
        )

        gravadas = conexao.execute(
            f"SELECT count(*) FROM read_parquet('{parcial.as_posix()}')"
        ).fetchone()
        arestas = int(gravadas[0]) if gravadas else 0
        try:
            validar_arestas_conservadas(vinculos, arestas)
            validar_indice_na_faixa(conexao, f"read_parquet('{parcial.as_posix()}')", nos)
        except ErroDeGrafo:
            parcial.unlink(missing_ok=True)
            raise

        lacos = conexao.execute(
            f"SELECT count(*) FROM read_parquet('{parcial.as_posix()}') WHERE no_empresa = no_socio"
        ).fetchone()

        # Os três números do colapso numa passagem só, porque saem do mesmo
        # agrupamento e varrer 8,7 milhões de linhas três vezes é desperdício.
        colapso = conexao.execute(
            f"""
            SELECT count(*),
                   count(*) FILTER (WHERE vinculos > 1),
                   count(*) FILTER (WHERE qualificacoes > 1)
            FROM (
              SELECT count(*) AS vinculos, count(DISTINCT qualificacao_socio) AS qualificacoes
              FROM read_parquet('{parcial.as_posix()}') GROUP BY no_empresa, no_socio
            )
            """
        ).fetchone()
        pares, paralelos, divergentes = (
            tuple(int(valor) for valor in colapso) if colapso else (0, 0, 0)
        )

    parcial.replace(destino)

    resultado = Arestas(
        caminho=destino,
        arestas=arestas,
        pares_distintos=pares,
        lacos=int(lacos[0]) if lacos else 0,
        pares_paralelos=paralelos,
        pares_com_qualificacao_divergente=divergentes,
        bytes_das_arestas=destino.stat().st_size,
    )
    logger.info(
        "arestas geradas",
        extra={
            "competencia": alvo,
            "uf_alvo": config.uf_alvo,
            "arestas": resultado.arestas,
            "pares_distintos": resultado.pares_distintos,
            "lacos": resultado.lacos,
            "pares_paralelos": resultado.pares_paralelos,
            "pares_com_qualificacao_divergente": resultado.pares_com_qualificacao_divergente,
            "nos": nos,
            "arquivo": destino.name,
            "bytes_arestas": resultado.bytes_das_arestas,
        },
    )
    return resultado


def _gravar(caminho: Path, array: np.ndarray[Any, np.dtype[Any]]) -> int:
    """Grava um `.npy` por arquivo temporário, para nunca deixar meio artefato.

    O descritor é aberto aqui de propósito: `np.save` acrescenta `.npy` ao nome
    quando ele não termina assim, e o temporário `indptr.npy.parcial` viraria
    `indptr.npy.parcial.npy`. Com o arquivo já aberto, ele grava onde mandaram.
    """
    parcial = caminho.with_name(f"{caminho.name}.parcial")
    with parcial.open("wb") as arquivo:
        np.save(arquivo, array, allow_pickle=False)
    parcial.replace(caminho)
    return caminho.stat().st_size


def serializar_csr(config: Config, competencia: str | None = None) -> Csr:
    """Transforma a lista de arestas em `indptr`, `indices` e o atributo paralelo.

    **O colapso é sobre o par não ordenado, e não sobre o par ordenado.** São 654
    casos no recorte em que A é sócia de B e B é sócia de A. Agrupar pelo par
    ordenado os manteria como dois registros, e a simetrização somaria mais dois —
    o mesmo vizinho apareceria **duas vezes** dentro da mesma linha. Ninguém
    reclamaria: `indptr` fecharia, o total bateria, e o grau desses 654 nós viria
    inflado. Colapsar pelo par não ordenado resolve na origem, e de quebra faz a
    qualificação ser a mesma nos dois sentidos por construção, que é justamente o
    que a simetria precisa afirmar.

    **A qualificação sobrevivente é o menor código.** Determinística e total,
    porque a largura do código é fixa. Ela só decide alguma coisa em 14 arestas —
    as que têm vínculos discordantes — e esse número vai no log de toda execução,
    inclusive quando é zero, para que o dia em que o colapso passar a escolher
    muito não passe despercebido.

    **Laço sai.** 9.049 vínculos de empresa consigo mesma. Não levam a lugar
    nenhum, e manter um deles somaria dois ao grau de um nó que não ganhou vizinho
    nenhum. Saem contados.
    """
    alvo = competencia or config.competencia
    destino = config.data_dir / "grafo" / alvo
    arestas_parquet = destino / "arestas.parquet"
    nos_parquet = destino / "nos.parquet"
    if not nos_parquet.exists():
        raise NosAusentesError(
            f"Não há nós em {nos_parquet}. O CSR tem uma linha por nó, então o conjunto de nós "
            "precisa existir antes — e ser o mesmo que gerou os índices das arestas."
        )
    if not arestas_parquet.exists():
        raise ArestasAusentesError(
            f"Não há arestas em {arestas_parquet}. O CSR é a serialização delas, e não uma "
            "segunda leitura do silver."
        )

    with abrir_conexao(config, config.data_dir / "duckdb-tmp") as conexao:
        conexao.execute("SET preserve_insertion_order=false")
        medida = conexao.execute(
            f"SELECT count(*) FROM read_parquet('{nos_parquet.as_posix()}')"
        ).fetchone()
        nos = int(medida[0]) if medida else 0

        conexao.execute(
            f"""
            CREATE OR REPLACE TEMP TABLE colapsada AS
            SELECT least(no_empresa, no_socio) AS a,
                   greatest(no_empresa, no_socio) AS b,
                   min(qualificacao_socio) AS qualificacao,
                   count(DISTINCT qualificacao_socio) AS qualificacoes
            FROM read_parquet('{arestas_parquet.as_posix()}')
            WHERE no_empresa <> no_socio
            GROUP BY 1, 2
            """
        )

        contagens = conexao.execute(
            f"""
            SELECT
              (SELECT count(*) FROM read_parquet('{arestas_parquet.as_posix()}')
               WHERE no_empresa = no_socio),
              (SELECT count(*) FROM colapsada),
              (SELECT count(*) FROM colapsada WHERE qualificacoes > 1),
              (SELECT count(*) FROM (
                 SELECT DISTINCT no_empresa, no_socio
                 FROM read_parquet('{arestas_parquet.as_posix()}')
                 WHERE no_empresa <> no_socio)),
              (SELECT count(*) FROM colapsada
               WHERE qualificacao IS NOT NULL AND TRY_CAST(qualificacao AS SMALLINT) IS NULL)
            """
        ).fetchone()
        lacos, arestas, divergentes, ordenados, nao_numericas = (
            tuple(int(valor) for valor in contagens) if contagens else (0, 0, 0, 0, 0)
        )
        if nao_numericas:
            raise QualificacaoNaoNumericaError(
                f"{nao_numericas:,} arestas têm qualificação que não é número e não cabe num "
                "array de inteiro. O código é texto na origem justamente porque a fonte é "
                "inconsistente; converter em silêncio descartaria o atributo dessas arestas."
            )

        # A simetrização é aqui, e o `ORDER BY` é o que produz o formato: ordenado
        # por linha, e por vizinho dentro da linha. Sem ele não há CSR, há uma
        # lista de pares.
        tabela = conexao.execute(
            f"""
            SELECT origem, destino, qualificacao FROM (
              SELECT a AS origem, b AS destino,
                     coalesce(TRY_CAST(qualificacao AS SMALLINT), {SENTINELA_DE_QUALIFICACAO})
                       AS qualificacao
              FROM colapsada
              UNION ALL
              SELECT b, a,
                     coalesce(TRY_CAST(qualificacao AS SMALLINT), {SENTINELA_DE_QUALIFICACAO})
              FROM colapsada
            )
            ORDER BY origem, destino
            """
        ).fetchnumpy()

    origem = np.ascontiguousarray(tabela["origem"], dtype=np.int64)
    indices = np.ascontiguousarray(tabela["destino"], dtype=np.int32)
    largura = np.ascontiguousarray(tabela["qualificacao"], dtype=np.int16)
    del tabela

    validar_qualificacao_cabe_em_int8(largura)
    qualificacoes = largura.astype(np.int8)
    del largura

    acumulado = np.cumsum(np.bincount(origem, minlength=nos))
    indptr = np.zeros(nos + 1, dtype=np.int32)
    validar_indice_cabe_em_int32(acumulado)
    indptr[1:] = acumulado
    del origem, acumulado

    validar_vizinhos_ordenados(indptr, indices)
    validar_simetria(indptr, indices, qualificacoes)

    bytes_totais = (
        _gravar(destino / "indptr.npy", indptr)
        + _gravar(destino / "indices.npy", indices)
        + _gravar(destino / "qualificacoes.npy", qualificacoes)
    )

    resultado = Csr(
        caminho_do_indptr=destino / "indptr.npy",
        caminho_dos_indices=destino / "indices.npy",
        caminho_das_qualificacoes=destino / "qualificacoes.npy",
        nos=nos,
        arestas=arestas,
        posicoes=int(indices.size),
        lacos_descartados=lacos,
        pares_com_qualificacao_divergente=divergentes,
        pares_mutuos=ordenados - arestas,
        grau_maximo=int(np.diff(indptr).max()) if nos else 0,
        bytes_totais=bytes_totais,
    )
    logger.info(
        "grafo serializado em CSR",
        extra={
            "competencia": alvo,
            "uf_alvo": config.uf_alvo,
            "nos": resultado.nos,
            "arestas": resultado.arestas,
            "posicoes": resultado.posicoes,
            "lacos_descartados": resultado.lacos_descartados,
            "pares_com_qualificacao_divergente": resultado.pares_com_qualificacao_divergente,
            "pares_mutuos": resultado.pares_mutuos,
            "grau_maximo": resultado.grau_maximo,
            "bytes_totais": resultado.bytes_totais,
        },
    )
    return resultado
