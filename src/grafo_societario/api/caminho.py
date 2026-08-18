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
| exemplos curados da demo | **0,02 ms** | 0,92 ms | 1,29 ms |
| 300 pares aleatórios do gigante, profundidade 10 | **0,27 ms** | 2,34 ms | 15,86 ms |
| os mesmos, profundidade 40 | 10,48 ms | 40,21 ms | 74,48 ms |
| os mesmos, profundidade 10, **pela pilha HTTP** | **1,39 ms** | 3,13 ms | 5,21 ms |

Duas leituras saem daí.

**No padrão, o framework custa mais que o grafo.** A consulta leva 0,27 ms e a
resposta inteira leva 1,39 ms: os outros 1,1 ms são ASGI, roteamento e
serialização HTTP. Otimizar a travessia a partir daqui não moveria o número que
o usuário sente.

**O padrão de profundidade também é o barato, e não foi escolhido por isso.** De
10 para 40 a mediana vai de 0,27 ms a 10,48 ms — quarenta vezes — porque a
mediana de distância é 20 e a busca que para no nível 10 nem chega perto. O
argumento do padrão é de significado, e está em `PROFUNDIDADE_PADRAO`; que o
custo concorde é conveniência, não justificativa.

Os desfechos daquela amostra confirmam a distribuição da Fase 5: com
profundidade 10, **293 dos 300 pares respondem `alem_do_limite`**; com 40, 297
respondem `encontrado`. É o formato deste grafo, e não uma falha da busca.
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

O custo de errar para baixo é **conhecido e barato**: 97,5% dos pares do gigante
estão além de 10 saltos, e todos recebem `alem_do_limite`, que não afirma
ausência e diz para aumentar o limite. Errar para cima entregaria caminho de 30
saltos com cara de descoberta.

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
    "societário afirma mais do que o dado sustenta. Pedir menos devolve `alem_do_limite`, que "
    "**não afirma ausência de vínculo**. Para cobrir o p99, peça 40."
)

DESCRICAO_DO_CNPJ: Final = (
    "CNPJ com os quatorze dígitos, com máscara ou sem. O `cnpj_basico` de oito dígitos **não é "
    "aceito**: ele não tem verificador, e um erro de digitação viraria consulta silenciosa a "
    "outra empresa."
)

roteador = APIRouter(tags=["consulta"])


def _saltos_por_extenso(saltos: int) -> str:
    return "1 salto" if saltos == 1 else f"{saltos} saltos"


def descrever(
    desfecho: DesfechoDaConsulta, saltos: int | None, profundidade_maxima: int
) -> tuple[bool, str]:
    """Se o desfecho afirma ausência de vínculo, e a frase que o explica.

    O booleano existe para o consumidor não precisar saber de cabeça quais dois
    dos cinco autorizam mostrar "não há vínculo". Errar isso é gratuito, e o erro
    é uma afirmação falsa sobre empresa real.
    """
    match desfecho:
        case DesfechoDaConsulta.ENCONTRADO:
            quanto = "" if saltos is None else f", com {_saltos_por_extenso(saltos)}"
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
            return False, (
                f"Existe caminho entre as duas empresas e ele é mais longo que os "
                f"{_saltos_por_extenso(profundidade_maxima)} pedidos. **Isto não diz que elas "
                "não têm vínculo** — aumente profundidade_maxima para procurar mais fundo."
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
        grau=acervo.grafo.grau(indice),
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

    encontrado = buscar_caminho(
        acervo.grafo, acervo.componentes, indice_de, indice_para, profundidade_maxima
    )
    desfecho = _da_travessia(encontrado.desfecho)
    return _montar(
        cnpj_de,
        cnpj_para,
        desfecho,
        profundidade_maxima,
        saltos=encontrado.saltos if encontrado.encontrado else None,
        caminho=[_no_da_resposta(acervo, indice) for indice in encontrado.nos],
        visitados=encontrado.visitados,
    )


def _montar(
    de: Cnpj,
    para: Cnpj,
    desfecho: DesfechoDaConsulta,
    profundidade_maxima: int,
    saltos: int | None = None,
    caminho: list[NoDaResposta] | None = None,
    visitados: int = 0,
) -> RespostaDeCaminho:
    afirma_ausencia, explicacao = descrever(desfecho, saltos, profundidade_maxima)
    return RespostaDeCaminho(
        desfecho=desfecho,
        afirma_ausencia=afirma_ausencia,
        explicacao=explicacao,
        de=de.completo,
        para=para.completo,
        saltos=saltos,
        caminho=caminho or [],
        profundidade_maxima=profundidade_maxima,
        visitados=visitados,
    )
