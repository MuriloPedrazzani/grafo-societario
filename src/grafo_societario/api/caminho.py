"""O endpoint de caminho societário, e a ordem em que ele decide.

## A ordem de checagem é a resposta, não um detalhe de implementação

Dois artefatos respondem perguntas diferentes, e a ordem em que são consultados
determina o que a API afirma:

1. **catálogo** — a empresa é nó do grafo? Sim → travessia, com os quatro
   desfechos da Fase 5.
2. **`existencia.npy`** — não é nó, mas está no recorte? → `sem_vinculo`. Ela
   existe e não tem vínculo nenhum, que é o caso de 74,8% do recorte.
3. **nenhum dos dois** → `404`. Não há o que dizer sobre esta empresa.

**O `404` é "não conheço", e não "fora do recorte".** A diferença tem 36.810
casos: o **conector** é pessoa jurídica de outra UF que entrou no grafo por ser
sócia de uma empresa daqui. Ele é nó, tem arestas, e **aparece dentro dos
caminhos que esta rota devolve** — recusá-lo na entrada faria a API emitir um
CNPJ numa resposta e rejeitar o mesmo CNPJ na requisição seguinte. Quem segue um
caminho salto a salto bateria nisso no primeiro conector.

Por isso a checagem de nó vem antes da de recorte, e não depois. O nó do conector
responde com `no_recorte: false`, que é o aviso de que o grau dele é piso: só
foram ingeridos os vínculos com empresas do recorte.

**O `404` vence o `sem_vinculo`, inclusive no caso misto.** Consultar uma empresa
sem vínculo contra uma desconhecida responde `404`, nos dois sentidos. O motivo é
que `sem_vinculo` significa "a empresa **existe** e não tem vínculo": emiti-lo
sobre esse par afirmaria de lado a existência da outra ponta, num campo que
ninguém lê como afirmação. O erro de digitação que produz um CNPJ de verificador
válido receberia "não têm vínculo" em vez de "não encontrei essa empresa".

## O limite é até onde o caminho é mostrado, não até onde a busca vai

A busca é sempre chamada **sem limite de profundidade**, com o orçamento de
visitados como único freio. `profundidade_maxima` é aplicado depois, sobre a
distância encontrada, e decide apenas se o trajeto é exibido.

Isso muda o que `alem_do_limite` significa. Antes ele dizia "não procurei até
lá" — ignorância. Agora diz **"há caminho, com 22 saltos, mais que os 10
pedidos"**: o vínculo existe, é remoto, e a distância é um achado verdadeiro. Com
o padrão de 10, esse é o desfecho de 293 em cada 300 pares do maior componente, e
seria muito pobre gastá-lo dizendo que não se sabe.

É também exatamente o que o projeto vem afirmando com números: **grafo societário
não é mundo pequeno**. A distância mediana é de 20 saltos, e cada resposta
`alem_do_limite` passa a ser mais uma medição disso em vez de uma desistência.

`orcamento_excedido` fica sendo o **único "não sei"** que resta.

O custo está medido na tabela abaixo: a mediana da consulta vai de 0,27 ms para
6,70 ms, e a resposta inteira de 1,39 ms para 8,01 ms. Os exemplos curados da
demo não pagam nada — eles são caminhos curtos ou pares de componentes
diferentes, e o rótulo de componente continua respondendo em 0,02 ms.

## Os cinco desfechos são exaustivos no tipo, e o mypy é quem cobra

A conversão de `graph.traversal.Desfecho` para `DesfechoDaConsulta` é um `match`
com `assert_never` no fim. Se a travessia ganhar um quinto desfecho algum dia, o
`match` deixa de ser exaustivo e **o mypy recusa o commit** — em vez de o desfecho
novo ser mapeado em silêncio para o mais parecido, que é como a distinção que a
Fase 5 construiu se perderia sem nada falhando.

É a versão em tipo da regra que este projeto aplica em dado: lista de permissão,
nunca de exclusão; entrar é decisão, sair é o padrão.

## A promessa dos milissegundos, medida

Travessia mais catálogo mais serialização, contra o artefato real de 2026-06 com
10.658.250 nós, depois do aquecimento:

| amostra | mediana | p95 | máximo |
|---|---:|---:|---:|
| exemplos curados da demo | **0,02 ms** | 0,87 ms | 0,94 ms |
| 300 pares aleatórios do gigante | **6,70 ms** | 37,69 ms | 72,44 ms |
| os mesmos, **pela pilha HTTP** | **8,01 ms** | 39,74 ms | 74,03 ms |

O que a demo exercita custa **0,02 ms**, e é isso que o visitante sente: caminho
curto responde pelo trajeto, par de componentes diferentes responde pelo rótulo
sem percorrer nada.

Os 6,70 ms da mediana são o preço de ir até o fim para saber a distância, e a
folga é larga: a busca toca **6.834 nós na mediana e 75.291 no máximo**, contra
um orçamento de 250.000. O pior caso continua limitado por construção, e não por
amostra.

Uma comparação que sobreviveu à mudança: o par de 4 saltos da demo custa 0,78 ms
de consulta dentro de uma resposta HTTP de poucos milissegundos. **Para caminho
curto, o framework ainda custa mais que o grafo** — o que muda o número do
usuário não é otimizar a travessia.
"""

from __future__ import annotations

import logging
from typing import Annotated, Final, assert_never

from fastapi import APIRouter, HTTPException, Query

from grafo_societario.api.cnpj import Cnpj, CnpjInvalidoError, analisar, formatar
from grafo_societario.api.deps import Acervo, AcervoDep
from grafo_societario.api.schemas import DesfechoDaConsulta, NoDaResposta, RespostaDeCaminho
from grafo_societario.graph.traversal import Desfecho, buscar_caminho

logger = logging.getLogger(__name__)

PROFUNDIDADE_PADRAO: Final = 10
"""Até quantos saltos procurar, quando ninguém pediu.

**É decisão de produto, não de custo.** O custo já tem dono: o orçamento de
visitados do commit 29 limita a busca por construção, e a pior consulta medida
coube em 107 ms. A profundidade responde outra pergunta — a partir de quantos
saltos isto deixa de significar alguma coisa.

A distribuição real, sobre 60.000 pares do maior componente: mediana **20**,
p95 32, p99 38, máximo observado 57. Os dois padrões óbvios foram recusados:

- **20**, a mediana, responde metade das consultas que têm caminho. Mas um
  caminho de 20 saltos atravessa dez empresas intermediárias, e chamar aquilo de
  vínculo societário afirma muito mais do que o dado sustenta.
- **40** cobre o p99 e quase sempre entrega um caminho — quase sempre um caminho
  sem significado. É o pior dos dois: o número que faz o serviço parecer útil
  ensinando o visitante a ler trama onde há topologia.

**10 é onde a resposta ainda é conferível por gente.** A cadeia tem cinco
empresas e cinco pessoas no meio, e quem lê consegue percorrer salto a salto. É a
mesma régua da curadoria da demo, que recusou caminho por nó de grau alto porque
coadministração pelo mesmo contador é aresta verdadeira e não é vínculo
societário significativo. Aquilo foi decisão de modelagem dita em voz alta; esta
é a mesma decisão, no padrão de um parâmetro.

**A assimetria dos erros é o que fecha a escolha.** Errar para baixo devolve
`alem_do_limite`, que não afirma ausência e — desde que a busca deixou de parar
no limite — vem **com a distância real**: "há caminho, com 22 saltos, mais que os
10 pedidos". É informação verdadeira, e não uma desistência. Errar para cima
entregaria trinta saltos com cara de descoberta, e essa perda não tem
recuperação: o leitor já leu.

A distribuição inteira vai na descrição do parâmetro, porque quem discordar
precisa dos mesmos números para discordar — e discordar aqui custa um parâmetro
de consulta.
"""

DESCRICAO_DA_PROFUNDIDADE: Final = (
    "Até quantos saltos procurar. **A intuição dos seis graus não vale aqui**: este grafo tem "
    "grau médio 2,79 e é quase arbóreo. Medido sobre 60.000 pares do maior componente, a "
    "distância mediana é de **20 saltos**, com p95 de **32**, p99 de **38** e máximo observado "
    f"de **57** — e apenas 0,55% dos pares estão a até 6 saltos. O padrão é "
    f"{PROFUNDIDADE_PADRAO}, escolhido por significado e não por custo: além de uma dezena de "
    "saltos a cadeia atravessa cinco empresas intermediárias, e chamar aquilo de vínculo "
    "societário afirma mais do que o dado sustenta. **Este limite decide até onde o caminho é "
    "mostrado, e não até onde a busca procura**: quando a distância excede o pedido, a resposta "
    "traz `alem_do_limite` com a distância real e sem o trajeto — o vínculo existe e é remoto. "
    "Para ver o trajeto desses casos, peça 40, que cobre o p99."
)

DESCRICAO_DO_CNPJ: Final = (
    "CNPJ com os quatorze dígitos, com máscara ou sem. O `cnpj_basico` de oito dígitos **não é "
    "aceito**: ele não tem verificador, e um erro de digitação viraria consulta silenciosa a "
    "outra empresa."
)

SEM_LIMITE_DE_PROFUNDIDADE: Final = 2**31 - 1
"""A profundidade com que a busca é sempre chamada: nenhuma.

`graph.traversal` exige o limite, e com razão — lá ele é obrigatório para que
ninguém herde um número da intuição. Aqui a resposta é explícita: **não limite a
busca**. Quem limita o custo é o orçamento de visitados, que é o papel dele desde
o commit 29, e a medição mostra que sobra folga larga: mediana de 6.834 nós
tocados e máximo de 75.291 sobre 300 pares do gigante, contra teto de 250.000.

O valor é grande e não infinito porque o parâmetro é `int` e a comparação é
`nivel_o + nivel_d + 1 > profundidade_maxima`. Nenhum caminho deste grafo chega
perto: o máximo observado na Fase 5 foi 57 saltos."""

roteador = APIRouter(tags=["consulta"])


def _saltos_por_extenso(saltos: int) -> str:
    return "1 salto" if saltos == 1 else f"{saltos} saltos"


def descrever(
    desfecho: DesfechoDaConsulta, distancia: int | None, profundidade_maxima: int
) -> tuple[bool, str]:
    """Se o desfecho afirma ausência de vínculo, e a frase que o explica.

    O booleano existe para o consumidor não precisar saber de cabeça quais dois
    dos cinco autorizam mostrar "não há vínculo". Errar isso é gratuito, e o erro
    é uma afirmação falsa sobre empresa real.
    """
    match desfecho:
        case DesfechoDaConsulta.ENCONTRADO:
            quanto = "" if distancia is None else f", com {_saltos_por_extenso(distancia)}"
            return False, f"Há caminho societário entre as duas empresas{quanto}."
        case DesfechoDaConsulta.SEM_VINCULO:
            return True, (
                "Ao menos uma das empresas existe no recorte e não tem vínculo societário "
                "nenhum registrado. É o caso de 74,8% das empresas: no empresário individual "
                "o dono está na razão social, e o projeto recusou deliberadamente extraí-lo."
            )
        case DesfechoDaConsulta.COMPONENTES_DIFERENTES:
            return True, (
                "As duas empresas têm vínculos e não se alcançam por caminho nenhum. A "
                "ausência é definitiva, e não depende do limite de profundidade."
            )
        case DesfechoDaConsulta.ALEM_DO_LIMITE:
            if distancia is None:
                return False, (
                    "Existe caminho entre as duas empresas e ele é mais longo que os "
                    f"{_saltos_por_extenso(profundidade_maxima)} pedidos. **Isto não diz que "
                    "elas não têm vínculo.**"
                )
            return False, (
                f"Há caminho societário entre as duas empresas, com "
                f"{_saltos_por_extenso(distancia)} — mais que os "
                f"{_saltos_por_extenso(profundidade_maxima)} pedidos, e por isso o caminho não "
                "é mostrado. **O vínculo existe e é remoto**; aumente profundidade_maxima para "
                "ver o trajeto."
            )
        case DesfechoDaConsulta.ORCAMENTO_EXCEDIDO:
            return False, (
                "Existe caminho entre as duas empresas e a busca desistiu antes de encontrá-lo, "
                "por ter gastado o orçamento de nós visitados. **Isto não diz que elas não têm "
                "vínculo** — diz que esta consulta parou no meio."
            )
        case _ as nao_tratado:
            assert_never(nao_tratado)


def _da_travessia(desfecho: Desfecho) -> DesfechoDaConsulta:
    """Converte o desfecho da Fase 5 no da resposta, sem colapsar nenhum.

    O `assert_never` é a guarda: desfecho novo na travessia faz este `match`
    deixar de ser exaustivo, e o mypy recusa antes de existir requisição.
    """
    match desfecho:
        case Desfecho.ENCONTRADO:
            return DesfechoDaConsulta.ENCONTRADO
        case Desfecho.COMPONENTES_DIFERENTES:
            return DesfechoDaConsulta.COMPONENTES_DIFERENTES
        case Desfecho.ALEM_DO_LIMITE:
            return DesfechoDaConsulta.ALEM_DO_LIMITE
        case Desfecho.ORCAMENTO_EXCEDIDO:
            return DesfechoDaConsulta.ORCAMENTO_EXCEDIDO
        case _ as nao_tratado:
            assert_never(nao_tratado)


def _no_da_resposta(acervo: Acervo, indice: int) -> NoDaResposta:
    """Um nó do catálogo na forma que a resposta mostra.

    O índice denso **não sai daqui**: ele é atribuído pela ordem do identificador
    e o conjunto de nós muda a cada competência, então uma resposta que o
    carregasse convidaria a rota `/no/12345`, que funcionaria hoje e devolveria
    outra empresa no mês seguinte, sem erro e com aparência de acerto.
    """
    cnpj_basico = acervo.catalogo.cnpj_basico_de(indice)
    return NoDaResposta(
        tipo=acervo.catalogo.tipo_de(indice),
        nome=acervo.catalogo.nome_de(indice),
        cnpj=None if cnpj_basico is None else formatar(int(cnpj_basico)),
        regiao_fiscal=acervo.catalogo.regiao_de(indice),
        confianca=acervo.catalogo.confianca_de(indice),
        no_recorte=acervo.catalogo.no_recorte_de(indice),
        vinculos_no_recorte=acervo.grafo.grau(indice),
    )


def _analisar_ou_422(texto: str, campo: str) -> Cnpj:
    try:
        return analisar(texto)
    except CnpjInvalidoError as erro:
        raise HTTPException(status_code=422, detail=f"{campo}: {erro}") from erro


@roteador.get(
    "/caminho",
    summary="Caminho societário entre duas empresas",
    response_model=RespostaDeCaminho,
)
def caminho(
    acervo: AcervoDep,
    de: Annotated[str, Query(description=DESCRICAO_DO_CNPJ, examples=["11.222.333/0001-81"])],
    para: Annotated[str, Query(description=DESCRICAO_DO_CNPJ, examples=["11222333000181"])],
    profundidade_maxima: Annotated[
        int, Query(ge=1, description=DESCRICAO_DA_PROFUNDIDADE)
    ] = PROFUNDIDADE_PADRAO,
) -> RespostaDeCaminho:
    """O caminho societário mais curto entre duas empresas, ou por que não há um.

    **Todos os cinco desfechos respondem `200`**, no campo `desfecho`. Dois deles
    afirmam ausência de vínculo e três não — o campo `afirma_ausencia` diz qual é
    o caso, para que a interface não precise saber de cabeça.

    `404` fica reservado a CNPJ ausente do recorte, e `422` a CNPJ malformado.
    """
    cnpj_de = _analisar_ou_422(de, "de")
    cnpj_para = _analisar_ou_422(para, "para")

    # A checagem de nó vem primeiro: o conector de outra UF é nó do grafo e não
    # está no recorte, e perguntar pelo recorte antes o expulsaria de uma rota que
    # devolve o CNPJ dele dentro dos caminhos.
    indice_de = acervo.catalogo.indice_de(cnpj_de.cnpj_basico)
    indice_para = acervo.catalogo.indice_de(cnpj_para.cnpj_basico)

    desconhecidos = [
        cnpj.completo
        for cnpj, indice in ((cnpj_de, indice_de), (cnpj_para, indice_para))
        if indice is None and not acervo.existe_no_recorte(cnpj.cnpj_basico)
    ]
    if desconhecidos:
        raise HTTPException(
            status_code=404,
            detail=(
                f"Nesta competência não há empresa com este CNPJ: {', '.join(desconhecidos)}. "
                f"O grafo cobre as empresas cuja matriz está em {acervo.config.uf_alvo}, mais as "
                "de outras UFs que aparecem como sócias delas."
            ),
        )

    if indice_de is None or indice_para is None:
        return _montar(cnpj_de, cnpj_para, DesfechoDaConsulta.SEM_VINCULO, profundidade_maxima)

    # A busca vai até o fim, com o orçamento como único freio. O limite pedido
    # decide o que é **mostrado**, e é aplicado depois — ver o topo do módulo.
    achado = buscar_caminho(
        acervo.grafo, acervo.componentes, indice_de, indice_para, SEM_LIMITE_DE_PROFUNDIDADE
    )
    desfecho = _da_travessia(achado.desfecho)
    distancia = achado.saltos if achado.encontrado else None
    mostrar = achado.encontrado and achado.saltos <= profundidade_maxima
    if achado.encontrado and not mostrar:
        desfecho = DesfechoDaConsulta.ALEM_DO_LIMITE

    return _montar(
        cnpj_de,
        cnpj_para,
        desfecho,
        profundidade_maxima,
        distancia=distancia,
        caminho=[_no_da_resposta(acervo, indice) for indice in achado.nos] if mostrar else [],
        visitados=achado.visitados,
    )


def _montar(
    de: Cnpj,
    para: Cnpj,
    desfecho: DesfechoDaConsulta,
    profundidade_maxima: int,
    distancia: int | None = None,
    caminho: list[NoDaResposta] | None = None,
    visitados: int = 0,
) -> RespostaDeCaminho:
    afirma_ausencia, explicacao = descrever(desfecho, distancia, profundidade_maxima)
    return RespostaDeCaminho(
        desfecho=desfecho,
        afirma_ausencia=afirma_ausencia,
        explicacao=explicacao,
        de=de.completo,
        para=para.completo,
        distancia=distancia,
        caminho=caminho or [],
        profundidade_maxima=profundidade_maxima,
        visitados=visitados,
    )
