"""Da borda até o índice do nó, e do índice de volta à resposta.

Três rotas fazem as mesmas duas perguntas — "este CNPJ é válido?" e "o que ele é
neste grafo?" —, e as respostas precisam ser idênticas nas três. Duplicar a ordem
de checagem em cada rota é como as três divergem no commit que mexe numa só, sem
nada falhar: cada uma continua respondendo, e passam a responder coisas
diferentes sobre a mesma empresa.

## A ordem de checagem é a resposta, não um detalhe de implementação

Dois artefatos respondem perguntas diferentes, e a ordem em que são consultados
determina o que a API afirma:

1. **catálogo** — a empresa é nó do grafo? Sim → há vínculos a percorrer.
2. **`existencia.npy`** — não é nó, mas está no recorte? → existe e não tem
   vínculo nenhum, que é o caso de 74,8% do recorte.
3. **nenhum dos dois** → `404`. Não há o que dizer sobre esta empresa.

**O `404` é "não conheço", e não "fora do recorte".** A diferença tem 36.810
casos: o **conector** é pessoa jurídica de outra UF que entrou no grafo por ser
sócia de uma empresa daqui. Ele é nó, tem arestas, e **aparece dentro dos
caminhos que a API devolve** — recusá-lo na entrada faria o serviço emitir um
CNPJ numa resposta e rejeitar o mesmo CNPJ na requisição seguinte. Quem segue um
caminho salto a salto bateria nisso no primeiro conector.

Por isso a checagem de nó vem antes da de recorte, e não depois. O nó do conector
responde com `no_recorte: false`, que é o aviso de que os vínculos dele são piso:
só foram ingeridos os que tocam empresas do recorte.

**O `404` vence o "sem vínculo" quando as duas condições se misturam.** Consultar
uma empresa sem vínculo contra uma desconhecida responde `404`, nos dois
sentidos, porque dizer "sem vínculo" sobre esse par afirmaria de lado a
existência da outra ponta, num campo que ninguém lê como afirmação.

## O índice denso não atravessa esta fronteira

Ele entra na `Ponta` e para aí. O índice é atribuído pela ordem do identificador
e o conjunto de nós muda a cada competência: uma resposta que o carregasse
convidaria a rota `/no/12345`, que funcionaria hoje e devolveria outra empresa no
mês seguinte, sem erro e com aparência de acerto.
"""

from __future__ import annotations

from dataclasses import dataclass

from fastapi import HTTPException

from grafo_societario.api.cnpj import Cnpj, CnpjInvalidoError, analisar, formatar
from grafo_societario.api.deps import Acervo
from grafo_societario.api.schemas import NoDaResposta


@dataclass(frozen=True)
class Ponta:
    """Um CNPJ do pedido, já resolvido contra os artefatos."""

    cnpj: Cnpj
    indice: int | None
    """Índice do nó no grafo, ou `None` quando a empresa existe e não é nó.

    **Interno.** Ver a nota sobre o índice denso no topo do módulo."""

    @property
    def tem_vinculo(self) -> bool:
        """Se a empresa é nó do grafo. Falso em 74,8% do recorte."""
        return self.indice is not None


def analisar_ou_422(texto: str, campo: str) -> Cnpj:
    """O CNPJ conferido, ou `422` dizendo o que corrigir.

    O nome do campo entra na mensagem porque as rotas de duas pontas precisam
    dizer **qual** das duas está errada.
    """
    try:
        return analisar(texto)
    except CnpjInvalidoError as erro:
        raise HTTPException(status_code=422, detail=f"{campo}: {erro}") from erro


def resolver(acervo: Acervo, *cnpjs: Cnpj) -> tuple[Ponta, ...]:
    """Resolve os CNPJs contra o catálogo e o recorte, ou levanta `404`.

    Recebe vários porque o `404` precisa nomear **todas** as pontas
    desconhecidas: descobrir uma de cada vez faria quem digitou dois CNPJs
    errados corrigir um, repetir a consulta e descobrir o outro.
    """
    pontas = tuple(
        Ponta(cnpj=cnpj, indice=acervo.catalogo.indice_de(cnpj.cnpj_basico)) for cnpj in cnpjs
    )
    desconhecidas = [
        ponta.cnpj.completo
        for ponta in pontas
        if ponta.indice is None and not acervo.existe_no_recorte(ponta.cnpj.cnpj_basico)
    ]
    if desconhecidas:
        raise HTTPException(
            status_code=404,
            detail=(
                f"Nesta competência não há empresa com este CNPJ: {', '.join(desconhecidas)}. "
                f"O grafo cobre as empresas cuja matriz está em {acervo.config.uf_alvo}, mais as "
                "de outras UFs que aparecem como sócias delas."
            ),
        )
    return pontas


def no_da_resposta(acervo: Acervo, indice: int) -> NoDaResposta:
    """Um nó do catálogo na forma que a resposta mostra.

    Custa **uma descompressão de bloco `zlib`** por nó com nome, medida em 0,35
    ms no artefato real. Num caminho isso é irrelevante — vinte e poucos nós —,
    e numa vizinhança é o custo dominante: 96% do tempo de uma resposta de 3.729
    nós. É o que faz o teto de nós ser orçamento de latência, e não de bytes.
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
