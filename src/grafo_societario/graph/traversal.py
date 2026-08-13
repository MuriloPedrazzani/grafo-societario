"""Caminho societário entre dois nós, por busca bidirecional em largura.

Este é o primeiro módulo do projeto que roda no **caminho de requisição**, e o
desenho todo sai disso: ele importa NumPy e `graph.csr`, e mais nada. Sem DuckDB,
sem SciPy. A imagem da Fase 8 responde consulta sobre arrays pré-computados e não
tem por que carregar o motor que os produziu nem a biblioteca científica que
conferiu a topologia. Há teste que abre um processo limpo e exige que nenhum dos
dois esteja carregado depois de importar este módulo.

## A resposta mais barata é a negativa, e ela não percorre nada

Dois nós em componentes diferentes **não podem** ter caminho. O rótulo de
componente responde isso comparando dois inteiros, e é o que acontece na maioria
das consultas: 87,39% dos nós estão fora do maior componente, e um par tirado ao
acaso quase sempre cai em componentes distintos.

Rodar uma busca até esgotar para descobrir que não há caminho é o desperdício mais
caro que este módulo poderia cometer — e é justamente o caso mais comum.

## "Não existe" e "não achei" são respostas diferentes

`COMPONENTES_DIFERENTES` é definitivo: não há caminho, ponto. `ALEM_DO_LIMITE`
diz que existe caminho — o componente garante — e que ele é mais longo que a
profundidade pedida.

Confundir os dois faria o serviço dizer "estas empresas não têm vínculo" quando a
verdade é "não procurei até lá". Seria afirmação falsa sobre empresa real, e é o
modo de falha que esta fase existe para não cometer.

## A distância típica aqui não é seis

A intuição dos "seis graus de separação" vem de rede densa. Esta não é: o maior
componente tem 1.343.694 nós com **grau médio 2,79**, o que o torna quase
arbóreo. Medido sobre 60.000 pares aleatórios dentro dele:

| | saltos |
|---|---:|
| mediana | **20** |
| p95 | 32 |
| p99 | 38 |
| máximo observado | 57 |

**Apenas 0,55% dos pares estão a até 6 saltos**, e 2,50% a até 10. Um limite
padrão herdado da intuição devolveria `ALEM_DO_LIMITE` para 99,45% das consultas
que chegam até aqui.

Por isso `profundidade_maxima` **não tem padrão** e é argumento obrigatório. Um
número errado aqui não falha alto: ele responde "não procurei" para o caso normal,
com aparência de resposta. Escolher o padrão é decisão de produto, e ela se toma
com esta tabela à vista — não no cabeçalho de uma função.

## O caminho é determinístico, e o par é canonizado

Mesma consulta, mesmos bytes. A ordem é total em cinco pontos: vizinhos crescentes
dentro da linha (invariante da serialização), fronteira em ordem crescente, pai é
quem descobre primeiro, nó de encontro pelo menor comprimento com desempate por
índice, e lado expandido pela fronteira menor com desempate pela origem.

A busca acontece sempre com o **menor índice como origem**, e o resultado é
invertido quando o pedido veio ao contrário. Assim `caminho(A, B)` é o reverso
exato de `caminho(B, A)` por construção, e não por coincidência: sem isso, quem
consultasse os dois sentidos veria caminhos diferentes e leria como defeito.

## Parar no primeiro encontro devolve caminho que não é mínimo

A fronteira é expandida **por nível inteiro**, e a interseção é examinada depois
que o nível fechou. Achado o encontro, o comprimento sai do **mínimo sobre todos**
os nós de encontro, e não do primeiro que apareceu: nós do mesmo nível deste lado
podem estar a profundidades diferentes do outro, e o primeiro da lista não é
necessariamente o mais curto. É o erro clássico da busca bidirecional, e ele
produz caminho válido — só não mínimo.

## A vizinhança tem dois regimes, e o teto existe para o segundo

Quantos nós há a até `k` saltos depende inteiramente de onde se parte. Medido
sobre 300 nós aleatórios do maior componente e sobre os 15 de maior grau:

| k | aleatório (mediana) | aleatório (p95) | maior grau (mediana) |
|---:|---:|---:|---:|
| 1 | 3 | 8 | **1.132** |
| 2 | 8 | 130 | **2.249** |
| 3 | 18 | 317 | **32.304** |
| 4 | 39 | 1.327 | **57.396** |
| 5 | 73 | 3.697 | **92.004** |

Três ordens de grandeza de diferença. De um nó comum a bola cresce devagar, porque
o grau médio é 2,79; de um hub ela estoura no primeiro salto, e um único deles a
até 5 saltos alcança 7% do componente inteiro.

Por isso `teto_de_nos` **não tem padrão**, pelo mesmo motivo de
`profundidade_maxima`: um número generoso para um regime é absurdo para o outro. E
há um segundo motivo — quem consome decide. Uma resposta de API aguenta milhares
de nós; um desenho legível na tela aguenta centenas. Fixar aqui embutiria a
necessidade de um consumidor no caminho do outro.

## O corte é por nível inteiro, e o que sobrou é dito

Devolver metade de um nível entrega subgrafo que **parece completo e não é**: o
consumidor vê alguns vizinhos de três saltos sem saber quais faltaram, e nada na
resposta o avisa. É a mesma falha que `ALEM_DO_LIMITE` evita no caminho, num lugar
mais discreto.

Então ou o nível cabe inteiro, ou não entra — e o resultado carrega quantos nós o
nível recusado teria. Esse número é informação por si só: diz o tamanho do que não
está sendo visto.

## O que volta é o subgrafo induzido, não a árvore de busca

A busca em largura produz naturalmente uma árvore, e **árvore esconde ciclo**. Mas
o ciclo é o achado: duas empresas que compartilham um segundo sócio aparecem
ligadas no subgrafo induzido e desconectadas na árvore.

Toda aresta entre nós devolvidos entra, inclusive as do mesmo nível. É a diferença
entre "quem está perto" e "como eles se ligam", e é a segunda que interessa a quem
investiga.

Os nós da borda mostram menos vizinhos do que têm — é inerente a qualquer recorte
por distância. Por isso cada nó vem com o seu **grau real no grafo inteiro**, que
sai de `indptr` e não custa byte nenhum de artefato: sem ele, contar as arestas
desenhadas daria um grau que parece verdadeiro e não é.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, replace
from enum import StrEnum
from typing import Any

import numpy as np

from grafo_societario.graph.csr import Grafo

logger = logging.getLogger(__name__)

SEM_PAI: int = -1
"""Marca a raiz de cada lado. Índice de nó nunca é negativo."""


class Desfecho(StrEnum):
    """Por que a busca terminou. As três respostas não podem virar uma só."""

    ENCONTRADO = "encontrado"
    COMPONENTES_DIFERENTES = "componentes_diferentes"
    """Definitivo: os dois nós não se alcançam por caminho nenhum."""

    ALEM_DO_LIMITE = "alem_do_limite"
    """Existe caminho — o componente garante — e ele é mais longo que o pedido."""


class ErroDeTravessia(RuntimeError):
    """Falha ao percorrer o grafo."""


class ArtefatosDiscordantesError(ErroDeTravessia):
    """Os componentes dizem que há caminho e a topologia diz que não."""


class PedidoInvalidoError(ErroDeTravessia):
    """Saltos negativos, ou teto que não comporta nem o nó de partida."""


@dataclass(frozen=True)
class Caminho:
    """O que a busca encontrou, e o que custou encontrar."""

    nos: tuple[int, ...]
    """Índices densos, da origem ao destino. Vazio quando não há caminho."""

    desfecho: Desfecho
    visitados: int
    """Nós tocados pelos dois lados. Zero quando a resposta saiu do rótulo de
    componente, sem travessia."""

    @property
    def saltos(self) -> int:
        """Arestas percorridas. Zero para o mesmo nó, e zero sem caminho — por
        isso `desfecho` é o que se testa, e não este número."""
        return max(0, len(self.nos) - 1)

    @property
    def encontrado(self) -> bool:
        return self.desfecho is Desfecho.ENCONTRADO


def _expandir(
    grafo: Grafo,
    fronteira: list[int],
    pai: dict[int, int],
    profundidade: dict[int, int],
    nivel: int,
) -> list[int]:
    """Avança um nível, e devolve a nova fronteira em ordem crescente.

    A ordem importa duas vezes. A fronteira chega ordenada e os vizinhos de cada
    nó já estão ordenados dentro da linha do CSR, então a sequência de descoberta
    é total — e como o pai só é atribuído na primeira descoberta, ele fica
    determinado. Ordenar a saída é o que mantém a propriedade no nível seguinte.

    `tolist()` materializa **a linha**, não o arquivo: são poucos vizinhos, e
    iterar escalar de NumPy em laço Python custa mais do que a conversão. A vista
    do mapeamento continua sendo o que evita ler os 66 MiB de `indices`.
    """
    nova: list[int] = []
    for no in fronteira:
        for vizinho in grafo.vizinhos(no).tolist():
            if vizinho not in profundidade:
                profundidade[vizinho] = nivel
                pai[vizinho] = no
                nova.append(vizinho)
    nova.sort()
    return nova


def _montar(
    encontro: int, pai_da_origem: dict[int, int], pai_do_destino: dict[int, int]
) -> tuple[int, ...]:
    """Costura os dois lados no nó de encontro."""
    esquerda: list[int] = []
    no = encontro
    while no != SEM_PAI:
        esquerda.append(no)
        no = pai_da_origem[no]
    esquerda.reverse()

    direita: list[int] = []
    no = pai_do_destino[encontro]
    while no != SEM_PAI:
        direita.append(no)
        no = pai_do_destino[no]

    return tuple(esquerda + direita)


def _buscar(grafo: Grafo, origem: int, destino: int, profundidade_maxima: int) -> Caminho:
    """A busca canônica, sempre com `origem < destino`."""
    pai_o: dict[int, int] = {origem: SEM_PAI}
    pai_d: dict[int, int] = {destino: SEM_PAI}
    prof_o: dict[int, int] = {origem: 0}
    prof_d: dict[int, int] = {destino: 0}
    fronteira_o = [origem]
    fronteira_d = [destino]
    nivel_o = nivel_d = 0

    while fronteira_o and fronteira_d:
        # O corte vem antes da expansão: gastar o nível para depois descobrir que
        # ele ultrapassou o limite é trabalho jogado fora.
        if nivel_o + nivel_d + 1 > profundidade_maxima:
            return Caminho((), Desfecho.ALEM_DO_LIMITE, len(prof_o) + len(prof_d))

        # A fronteira menor é a que cresce menos ao ser expandida. O empate vai
        # para a origem, e não para o lado que por acaso estiver na variável.
        pela_origem = len(fronteira_o) <= len(fronteira_d)
        if pela_origem:
            nivel_o += 1
            fronteira_o = _expandir(grafo, fronteira_o, pai_o, prof_o, nivel_o)
            encontros = [no for no in fronteira_o if no in prof_d]
        else:
            nivel_d += 1
            fronteira_d = _expandir(grafo, fronteira_d, pai_d, prof_d, nivel_d)
            encontros = [no for no in fronteira_d if no in prof_o]

        if encontros:
            # Mínimo sobre TODOS os encontros. O primeiro da lista dá caminho
            # válido e não necessariamente mínimo, porque nós do mesmo nível deste
            # lado podem estar a profundidades diferentes do outro.
            melhor = min(encontros, key=lambda no: (prof_o[no] + prof_d[no], no))
            saltos = prof_o[melhor] + prof_d[melhor]
            visitados = len(prof_o) + len(prof_d)
            if saltos > profundidade_maxima:
                return Caminho((), Desfecho.ALEM_DO_LIMITE, visitados)
            return Caminho(_montar(melhor, pai_o, pai_d), Desfecho.ENCONTRADO, visitados)

    raise ArtefatosDiscordantesError(
        f"Os nós {origem:,} e {destino:,} estão no mesmo componente e a busca esgotou sem "
        "encontrá-los. O rótulo de componente e a topologia vieram de execuções diferentes, e "
        "a resposta negativa que sai do rótulo deixou de valer."
    )


def buscar_caminho(
    grafo: Grafo,
    componentes: np.ndarray[Any, np.dtype[np.int32]],
    origem: int,
    destino: int,
    profundidade_maxima: int,
) -> Caminho:
    """O caminho societário mais curto entre dois nós, ou por que não há um.

    `profundidade_maxima` é obrigatório de propósito. A distância mediana dentro
    do maior componente é **20 saltos**, e um limite herdado da intuição de "seis
    graus" responderia `ALEM_DO_LIMITE` a 99,45% das consultas — sem erro, com
    aparência de resposta. Ver a tabela no topo do módulo.

    A busca só acontece quando o rótulo de componente não resolve, e ela é feita
    sobre o par canônico: menor índice como origem, resultado invertido se o
    pedido veio ao contrário.
    """
    grafo.grau(origem)
    grafo.grau(destino)

    if origem == destino:
        return Caminho((origem,), Desfecho.ENCONTRADO, 1)
    if int(componentes[origem]) != int(componentes[destino]):
        return Caminho((), Desfecho.COMPONENTES_DIFERENTES, 0)
    if profundidade_maxima < 1:
        return Caminho((), Desfecho.ALEM_DO_LIMITE, 0)

    invertido = origem > destino
    menor, maior = (destino, origem) if invertido else (origem, destino)
    caminho = _buscar(grafo, menor, maior, profundidade_maxima)
    if invertido and caminho.nos:
        caminho = replace(caminho, nos=tuple(reversed(caminho.nos)))
    return caminho


@dataclass(frozen=True)
class Vizinhanca:
    """O subgrafo induzido a até `saltos` do nó de partida."""

    origem: int

    nos: tuple[int, ...]
    """Índices densos, em ordem crescente. Inclui a origem, na posição do seu índice."""

    profundidades: tuple[int, ...]
    """Distância de cada nó até a origem, paralelo a `nos`."""

    graus: tuple[int, ...]
    """Grau de cada nó **no grafo inteiro**, paralelo a `nos`.

    Não é o grau dentro deste subgrafo. Nó da borda tem vizinhos que ficaram de
    fora do recorte, e contar as arestas devolvidas daria um número que parece o
    grau e não é. Sai de `indptr` e não custa byte de artefato."""

    arestas: tuple[tuple[int, int], ...]
    """Toda aresta entre nós devolvidos, cada uma uma vez, com o menor índice à
    esquerda e em ordem crescente. **Induzidas, não a árvore de busca**: as do
    mesmo nível entram, e são elas que revelam ciclo."""

    saltos: int
    """Profundidade do nível mais fundo presente."""

    saltos_pedidos: int

    nivel_recusado: int
    """Quantos nós teria o primeiro nível que não coube. Zero quando nada foi
    recusado — inclusive quando a busca parou por ter esgotado o componente."""

    @property
    def truncada(self) -> bool:
        """Se algum nível foi recusado por teto.

        **Não é `saltos < saltos_pedidos`.** Um componente pequeno se esgota antes
        do `k` pedido e a resposta continua completa: tudo o que existe até lá está
        ali. Confundir os dois faria uma resposta inteira ser anunciada como
        parcial."""
        return self.nivel_recusado > 0


def vizinhanca(grafo: Grafo, origem: int, saltos: int, teto_de_nos: int) -> Vizinhanca:
    """Os nós a até `saltos` da origem, e todas as arestas entre eles.

    `teto_de_nos` é obrigatório: a bola cresce de 3 nós a 1.132 no primeiro salto
    conforme o ponto de partida, e não há número que sirva aos dois regimes. Ver a
    tabela medida no topo do módulo.

    O corte é por **nível inteiro**. Se o próximo nível não couber, ele não entra e
    `nivel_recusado` diz de que tamanho ele era — devolver parte dele entregaria um
    subgrafo que parece completo sem ser.
    """
    grafo.grau(origem)
    if saltos < 0:
        raise PedidoInvalidoError(f"Saltos não pode ser negativo: veio {saltos}.")
    if teto_de_nos < 1:
        raise PedidoInvalidoError(
            f"O teto precisa comportar ao menos o nó de partida: veio {teto_de_nos}."
        )

    profundidade = {origem: 0}
    fronteira = [origem]
    nivel_recusado = 0
    alcancado = 0

    for nivel in range(1, saltos + 1):
        # O nível é montado inteiro **antes** de ser aceito: marcar durante a
        # coleta obrigaria a desfazer a marcação se ele não coubesse, e desfazer
        # pela metade é como se devolve nível parcial sem perceber.
        proximo: list[int] = []
        no_nivel: set[int] = set()
        for no in fronteira:
            for vizinho in grafo.vizinhos(no).tolist():
                if vizinho not in profundidade and vizinho not in no_nivel:
                    no_nivel.add(vizinho)
                    proximo.append(vizinho)
        if not proximo:
            break
        if len(profundidade) + len(proximo) > teto_de_nos:
            nivel_recusado = len(proximo)
            break
        for no in proximo:
            profundidade[no] = nivel
        proximo.sort()
        fronteira = proximo
        alcancado = nivel

    nos = sorted(profundidade)
    conjunto = set(nos)
    # Já sai ordenado: `nos` é crescente e a linha do CSR também, o que dispensa
    # ordenar depois. É o mesmo invariante que o commit 23 afirmou.
    arestas = tuple(
        (no, vizinho)
        for no in nos
        for vizinho in grafo.vizinhos(no).tolist()
        if vizinho > no and vizinho in conjunto
    )

    resultado = Vizinhanca(
        origem=origem,
        nos=tuple(nos),
        profundidades=tuple(profundidade[no] for no in nos),
        graus=tuple(grafo.grau(no) for no in nos),
        arestas=arestas,
        saltos=alcancado,
        saltos_pedidos=saltos,
        nivel_recusado=nivel_recusado,
    )
    logger.info(
        "vizinhança calculada",
        extra={
            "origem": origem,
            "saltos_pedidos": saltos,
            "saltos": alcancado,
            "nos": len(nos),
            "arestas": len(arestas),
            "truncada": resultado.truncada,
            "nivel_recusado": nivel_recusado,
        },
    )
    return resultado
