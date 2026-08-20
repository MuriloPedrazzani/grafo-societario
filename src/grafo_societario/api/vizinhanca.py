"""O subgrafo em volta de uma empresa, e por que o teto é orçamento de latência.

## Dois regimes separados por três ordens de grandeza

Quantos nós há a até `k` saltos depende inteiramente de onde se parte. Medido
sobre 300 nós aleatórios e sobre os 15 de maior grau, **sem teto**:

| k | aleatório (mediana) | aleatório (p95) | maior grau (mediana) |
|---:|---:|---:|---:|
| 1 | 2 | 4 | **1.132** |
| 2 | 3 | 17 | **2.249** |
| 3 | 3 | 30 | **32.304** |
| 4 | 3 | 50 | **57.396** |

De um nó comum a bola cresce devagar, porque o grau médio é 2,79. De um hub ela
estoura no primeiro salto, e a até 4 saltos alcança 4% do grafo inteiro.

## O teto não limita bytes, limita tempo

A intuição era que uma resposta de API aguenta milhares de nós e o limite seria o
tamanho do JSON. **A medição desmentiu.** 3.729 nós dão 628 KB, que é tranquilo —
e custam **1,4 s**. A decomposição, sobre o maior hub:

| etapa | tempo |
|---|---:|
| travessia | 17,8 ms |
| **descompressão dos nomes** | **1.323,4 ms — 96%** |
| serialização JSON | 14,2 ms |

O custo é **uma descompressão de bloco `zlib` por nó com nome**, a 0,35 ms. O
teto de nós é, portanto, um orçamento de latência com conversão linear: 1.000 nós
são ~350 ms, 5.000 são ~1,75 s.

Ler por bloco em vez de por nó foi medido e **não resolve**: os 3.728 nomes de um
hub tocam 2.037 blocos distintos, então o teto de ganho é 1,8 vezes. O caminho para
mudar isso passaria pelo formato do artefato, não pelo código de leitura.

## Por que 1.000, e o que cada regime recebe com ele

`TETO_DE_NOS_PADRAO` é 1.000: cobre o regime aleatório com folga de 30 vezes
sobre o p95, e é o maior valor cujo pior caso fica abaixo de ~350 ms. Medido com
os padrões aplicados, 300 empresas aleatórias contra as 30 de maior grau:

| regime | nós mediana | nós p95 | ms mediana | ms p95 | ms máximo | truncadas |
|---|---:|---:|---:|---:|---:|---:|
| aleatório | 3 | 17 | 0,82 | 5,90 | 81,40 | 1 de 300 |
| grau alto | 747 | 991 | 22,85 | 283,87 | 321,68 | **28 de 30** |

Pela pilha HTTP: 2,70 ms de mediana no regime aleatório, 24,03 ms no de grau
alto. O pior caso do padrão é de 320 ms, e a previsão feita antes de medir era
de 350 — o teto se comporta como orçamento linear, como esperado.

**Para hub nenhum valor funciona**, e é por isso que o padrão não tenta agradar
os dois: 28 das 30 empresas de maior grau têm um nível recusado, e a resposta diz
em `nivel_recusado` de que tamanho ele era. **Falha rápida com informação vence
acerto lento** — quem quiser o nível inteiro pede um teto maior sabendo
exatamente quanto pedir, porque o número está lá.

Padrão de API serve consumidor de API. A página da Fase 7 desenha, e desenho
legível aguenta centenas — ela passa o teto dela, com esta tabela à vista.

## O corte é por nível inteiro

Devolver metade de um nível entrega um subgrafo que **parece completo e não é**:
o consumidor vê alguns vizinhos de dois saltos sem saber quais faltaram, e nada
na resposta o avisa. É a mesma falha que `alem_do_limite` evita no caminho, num
lugar mais discreto.
"""

from __future__ import annotations

import logging
from typing import Annotated, Final

from fastapi import APIRouter, Query

from grafo_societario.api.deps import AcervoDep
from grafo_societario.api.resolucao import analisar_ou_422, no_da_resposta, resolver
from grafo_societario.api.schemas import NoDaVizinhanca, RespostaDeVizinhanca
from grafo_societario.api.texto import milhar
from grafo_societario.graph.traversal import vizinhanca

logger = logging.getLogger(__name__)

SALTOS_PADRAO: Final = 2
"""Empresa, sócio, e as outras empresas do sócio.

É a unidade que significa alguma coisa — as *empresas irmãs* —, e é o primeiro
`k` em que o subgrafo induzido pode mostrar ciclo, que é o achado que a árvore de
busca esconderia. Os exemplos de vizinhança colhidos para a demo são todos de
dois saltos, pelo mesmo motivo."""

TETO_DE_NOS_PADRAO: Final = 1000
"""Teto de nós devolvidos. Ver o topo do módulo: é orçamento de latência."""

DESCRICAO_DO_TETO: Final = (
    "Máximo de nós na resposta. **É orçamento de latência, e não de bytes**: cada nó com nome "
    "custa uma descompressão de bloco, medida em 0,35 ms, e ela é 96% do tempo de uma resposta "
    "grande. A bola tem dois regimes separados por três ordens de grandeza, e **a mediana de um "
    "esconde o outro inteiro**. Medido com estes padrões contra o dado real: partindo de uma "
    "empresa aleatória a resposta tem **3 nós na mediana e 17 no p95**, em 0,82 ms; partindo de "
    "uma das de maior grau tem **747 na mediana e 991 no p95**, em 22,85 ms de mediana e 284 ms "
    "de p95 — e 28 de 30 delas têm um nível recusado. Sem teto nenhum, o primeiro salto de um "
    "hub sozinho chega a **1.132** nós e a bola a 3 saltos a **32.304**. Por isso nenhum valor "
    "serve aos dois regimes: recusar um nível em milissegundos, dizendo em `nivel_recusado` de "
    "que tamanho ele era, vence entregá-lo em mais de um segundo. Este padrão serve consumidor "
    "de API; quem desenha na tela aguenta centenas e deve passar o próprio."
)

DESCRICAO_DOS_SALTOS: Final = (
    "Até quantos saltos a partir da empresa. O padrão é 2 — empresa, sócio, e as outras "
    "empresas do sócio —, que é a unidade que significa alguma coisa e o primeiro valor em que "
    "o subgrafo pode mostrar ciclo. O corte é por **nível inteiro**: se o próximo não couber no "
    "teto, ele não entra e `nivel_recusado` diz de que tamanho ele era."
)

DESCRICAO_DO_CNPJ: Final = (
    "CNPJ com os quatorze dígitos, com máscara ou sem. O `cnpj_basico` de oito dígitos **não é "
    "aceito**: ele não tem verificador, e um erro de digitação viraria consulta silenciosa a "
    "outra empresa."
)

SEM_VINCULO: Final = (
    "Esta empresa existe no recorte e não tem vínculo societário nenhum registrado, então não "
    "há vizinhança a mostrar. É o caso de 74,8% das empresas: no empresário individual o dono "
    "está na razão social, e o projeto recusou deliberadamente extraí-lo."
)

roteador = APIRouter(tags=["consulta"])


@roteador.get(
    "/vizinhanca",
    summary="Subgrafo societário em volta de uma empresa",
    response_model=RespostaDeVizinhanca,
)
def consultar_vizinhanca(
    acervo: AcervoDep,
    cnpj: Annotated[str, Query(description=DESCRICAO_DO_CNPJ, examples=["11.222.333/0001-81"])],
    saltos: Annotated[int, Query(ge=0, description=DESCRICAO_DOS_SALTOS)] = SALTOS_PADRAO,
    teto_de_nos: Annotated[int, Query(ge=1, description=DESCRICAO_DO_TETO)] = TETO_DE_NOS_PADRAO,
) -> RespostaDeVizinhanca:
    """Quem está a até `saltos` da empresa, e **todas as arestas entre eles**.

    O que volta é o subgrafo **induzido**, e não a árvore de busca: as arestas do
    mesmo nível entram, e são elas que revelam ciclo — duas empresas que
    compartilham um segundo sócio aparecem ligadas aqui e desconectadas numa
    árvore.
    """
    analisado = analisar_ou_422(cnpj, "cnpj")
    (ponta,) = resolver(acervo, analisado)

    if ponta.indice is None:
        return RespostaDeVizinhanca(
            cnpj=analisado.completo,
            tem_vinculo=False,
            explicacao=SEM_VINCULO,
            nos=[],
            arestas=[],
            saltos_pedidos=saltos,
            saltos=0,
            teto_de_nos=teto_de_nos,
            truncada=False,
            nivel_recusado=0,
        )

    achado = vizinhanca(acervo.grafo, ponta.indice, saltos, teto_de_nos)
    # A aresta viaja como par de posições nesta resposta. O índice denso é
    # atribuído pela ordem do identificador e muda a cada competência — ver
    # `api.resolucao`.
    posicao = {no: lugar for lugar, no in enumerate(achado.nos)}

    return RespostaDeVizinhanca(
        cnpj=analisado.completo,
        tem_vinculo=True,
        explicacao=_explicar(len(achado.nos), achado.saltos, achado.nivel_recusado),
        nos=[
            NoDaVizinhanca(
                **no_da_resposta(acervo, no, lugar).model_dump(),
                profundidade=profundidade,
            )
            for lugar, (no, profundidade) in enumerate(
                zip(achado.nos, achado.profundidades, strict=True)
            )
        ],
        arestas=[(posicao[de], posicao[para]) for de, para in achado.arestas],
        saltos_pedidos=achado.saltos_pedidos,
        saltos=achado.saltos,
        teto_de_nos=teto_de_nos,
        truncada=achado.truncada,
        nivel_recusado=achado.nivel_recusado,
    )


def _explicar(quantos_nos: int, saltos: int, nivel_recusado: int) -> str:
    nos = "1 nó" if quantos_nos == 1 else f"{milhar(quantos_nos)} nós"
    ate = (
        "só a própria empresa"
        if saltos == 0
        else f"a até {saltos} salto{'' if saltos == 1 else 's'}"
    )
    if nivel_recusado:
        return (
            f"{nos}, {ate}. O nível seguinte tem {milhar(nivel_recusado)} nós e **não coube no "
            "teto**, "
            "então não entrou inteiro: meio nível entregaria um subgrafo que parece completo sem "
            "ser. Aumente teto_de_nos para vê-lo."
        )
    return f"{nos}, {ate}. Nada foi recusado por teto: é tudo o que existe até essa distância."
