"""A forma da resposta, e o campo que impede o consumidor de errar sozinho.

## Cinco desfechos, todos `200`, em campo de primeiro nível

A travessia da Fase 5 distingue quatro finais e **só um deles afirma ausência**.
A API acrescenta um quinto, que não vem da travessia: `sem_vinculo`, que sai de
`existencia.npy` combinado com "esta empresa não é nó do grafo".

| desfecho | significa | afirma ausência? |
|---|---|---|
| `encontrado` | achou caminho, e ele cabe no limite pedido | — |
| `sem_vinculo` | a empresa existe no recorte e não tem vínculo nenhum | sim |
| `componentes_diferentes` | as duas têm vínculos e não se alcançam | sim |
| `alem_do_limite` | há caminho, **a esta distância**, mais longo que o pedido | **não** |
| `orcamento_excedido` | há caminho, a busca desistiu antes | **não** |

`alem_do_limite` **afirma presença**, e não ignorância: a busca vai até o fim, e
o que o limite decide é se o caminho é exibido. `orcamento_excedido` é o único
"não sei" que sobrou.

Nenhum deles é erro de HTTP. `404` fica reservado a CNPJ ausente do recorte, que
é outra coisa: o pedido referencia empresa que não existe.

## `afirma_ausencia` existe porque a distinção é fácil de perder

Os nomes dos desfechos são explícitos, e ainda assim quem escreve uma interface
tem de saber de cabeça quais dois dos cinco autorizam mostrar "não há vínculo".
Errar isso é gratuito: `alem_do_limite` renderizado como "sem vínculo" é uma
afirmação falsa sobre empresa real, entregue com cara de resposta.

O campo booleano tira a decisão do consumidor. Junto vai `explicacao`, em
português, porque a interface da Fase 7 precisa de uma frase e inventá-la seria
inventar a semântica de novo, do lado de fora.

## O nome do campo é a última chance de a ressalva chegar

`vinculos_no_recorte` não se chama `grau`, e a diferença não é de estilo. Só
foram ingeridos sócios de empresas cuja matriz está na UF alvo, então o número é
**piso e nunca total**: quem participa de 3 empresas em SP e 40 no Rio aparece
com 3.

A coluna nasceu com esse nome no commit 19, o README tem uma seção sobre isso, e
a distinção atravessou três fases. `"grau": 3` num JSON se lê como "tem 3
sócios", que é falso — e a serialização seria o último metro, onde ninguém mais
teria como corrigir. Vale igual para tamanho de componente: é componente
**dentro do recorte**, e o nome do campo diz isso.

## `sem_vinculo` não pode virar `componentes_diferentes`

Os dois afirmam ausência, e por isso a tentação de fundi-los. Mas
`componentes_diferentes` diz que a empresa **tem** vínculos e eles não chegam à
outra; `sem_vinculo` diz que ela não tem vínculo nenhum. O segundo descreve 74,8%
do recorte — é o efeito do empresário individual, cujo dono está na razão social
e que o projeto recusou deliberadamente extrair. Apagar essa distinção apagaria o
custo medido daquela decisão.
"""

from __future__ import annotations

from enum import StrEnum

from pydantic import BaseModel, Field


class DesfechoDaConsulta(StrEnum):
    """Os cinco finais possíveis de uma consulta de caminho.

    São os quatro de `graph.traversal.Desfecho` mais `SEM_VINCULO`, que a API
    produz sozinha. A conversão entre os dois conjuntos é um `match` exaustivo,
    conferido pelo mypy: desfecho novo na travessia não compila até ser tratado
    aqui.
    """

    ENCONTRADO = "encontrado"
    SEM_VINCULO = "sem_vinculo"
    COMPONENTES_DIFERENTES = "componentes_diferentes"
    ALEM_DO_LIMITE = "alem_do_limite"
    ORCAMENTO_EXCEDIDO = "orcamento_excedido"


class NoDaResposta(BaseModel):
    """Um nó do caminho: empresa, pessoa física ou sócio estrangeiro."""

    tipo: str = Field(description="`pessoa_juridica`, `pessoa_fisica` ou `estrangeiro`.")
    nome: str | None = Field(
        description="Razão social, para pessoa jurídica. **Nulo para pessoa física e para "
        "estrangeiro**: o nome não entra no artefato publicado, e a decisão é da geração, "
        "não da resposta."
    )
    cnpj: str | None = Field(
        description="CNPJ completo da matriz, com o verificador calculado. Nulo para quem "
        "não é pessoa jurídica."
    )
    regiao_fiscal: str | None = Field(
        description="Dígito da região fiscal do CPF, de pessoa física. Substitui a máscara do "
        "CPF, que era chave de junção de volta à fonte pública."
    )
    confianca: str = Field(
        description="Como a identidade deste nó foi estabelecida: `exata` para pessoa "
        "jurídica, `estimada` para pessoa física, `fraca` para estrangeiro — que não tem "
        "documento nenhum, e cuja fusão é materialmente mais frágil — e `nao_fundivel` para "
        "sócio sem nome."
    )
    no_recorte: bool | None = Field(
        description="Se a empresa tem matriz na UF do recorte. Falso identifica o **conector**: "
        "empresa de outra UF que aparece por ser sócia de uma daqui, e cujos demais vínculos "
        "não foram ingeridos."
    )
    vinculos_no_recorte: int = Field(
        description="Vínculos deste nó **dentro do recorte**. É **piso, nunca total**: só foram "
        "ingeridos sócios de empresas cuja matriz está na UF alvo, então quem participa de 3 "
        "empresas em SP e 40 no Rio aparece aqui com 3. O campo não se chama `grau` de "
        "propósito — `grau` se lê como número absoluto, e este não é."
    )


class RespostaDeCaminho(BaseModel):
    """O caminho societário entre duas empresas, ou por que não há um."""

    desfecho: DesfechoDaConsulta
    afirma_ausencia: bool = Field(
        description="Se este desfecho autoriza dizer que não há vínculo. Verdadeiro apenas em "
        "`sem_vinculo` e `componentes_diferentes`. Nos outros dois negativos o caminho "
        "**existe** e esta busca não o entregou."
    )
    explicacao: str = Field(description="O desfecho em uma frase, para a interface exibir.")
    de: str
    para: str
    distancia: int | None = Field(
        description="Distância entre as duas empresas, em saltos. Conhecida em `encontrado` e "
        "**também em `alem_do_limite`**, onde o caminho não é mostrado mas a distância é um "
        "achado verdadeiro: o vínculo existe e é remoto. Nula quando não há caminho ou quando "
        "a busca desistiu."
    )
    caminho: list[NoDaResposta] = Field(
        description="Os nós, da origem ao destino. Preenchido **apenas** em `encontrado` — em "
        "`alem_do_limite` a distância é conhecida e o caminho não é exibido."
    )
    profundidade_maxima: int = Field(
        description="O limite pedido. Ele governa **até onde o caminho é mostrado**, e não até "
        "onde a busca procura: a busca vai até o fim, com o orçamento de visitados como único "
        "freio."
    )
    visitados: int = Field(
        description="Nós tocados pela travessia. Zero quando a resposta saiu do rótulo de "
        "componente, sem percorrer nada."
    )
