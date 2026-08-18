"""A forma da resposta, e o campo que impede o consumidor de errar sozinho.

## Cinco desfechos, todos `200`, em campo de primeiro nível

A travessia da Fase 5 distingue quatro finais e **só um deles afirma ausência**.
A API acrescenta um quinto, que não vem da travessia: `sem_vinculo`, que sai de
`existencia.npy` combinado com "esta empresa não é nó do grafo".

| desfecho | significa | afirma ausência? |
|---|---|---|
| `encontrado` | achou caminho | — |
| `sem_vinculo` | a empresa existe no recorte e não tem vínculo nenhum | sim |
| `componentes_diferentes` | as duas têm vínculos e não se alcançam | sim |
| `alem_do_limite` | há caminho, mais longo que o pedido | **não** |
| `orcamento_excedido` | há caminho, a busca desistiu antes | **não** |

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
    grau: int = Field(
        description="Vínculos deste nó **dentro do recorte**. É piso, nunca total: quem "
        "participa de 3 empresas em SP e 40 no Rio aparece aqui com 3."
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
    saltos: int | None = Field(
        description="Arestas do caminho. Nulo quando não há caminho — e não zero, que é a "
        "resposta legítima de uma empresa para ela mesma."
    )
    caminho: list[NoDaResposta] = Field(
        description="Da origem ao destino. Vazio em todo desfecho que não seja `encontrado`."
    )
    profundidade_maxima: int = Field(description="O limite efetivamente usado nesta busca.")
    visitados: int = Field(
        description="Nós tocados pela travessia. Zero quando a resposta saiu do rótulo de "
        "componente, sem percorrer nada."
    )
