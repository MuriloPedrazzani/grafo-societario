"""O que se sabe sobre uma empresa, sem os vizinhos dela.

## Por que esta rota não devolve a vizinhança

Não é recorte diferente do mesmo domínio: **é outro domínio**. As 14,8 milhões de
empresas do recorte que não têm vínculo nenhum não são nós do grafo, e sobre elas
`/vizinhanca` não teria o que devolver — esta rota responde. Se ela também
trouxesse os vizinhos, viraria `/vizinhanca?saltos=1` com outro nome, e duas
rotas que devolvem a mesma coisa divergem no primeiro commit que mexe numa só,
sem nada falhar.

## O nome ausente é dito, não omitido

Empresa sem vínculo responde com `nome` nulo, e isso descreve **74,8% do
recorte** — o caso majoritário, não a exceção. Uma resposta com nome nulo e nada
explicando é a única coisa aqui que um usuário leria como dado faltando ou como
defeito, então o motivo vai no corpo: o artefato publicado carrega apenas os nós
do grafo, e empresa sem vínculo não é nó.

**É limitação medida, e não pendência.** O catálogo guarda 63,6 MB de razão
social para os 5,02 milhões de nós; estender às 19,77 milhões do recorte custaria
cerca de 250 MB pela mesma escala, um acréscimo de ~186 MB sobre um artefato que
está em 416,1 MB contra teto de 500. Não cabe, e nenhum trabalho futuro faz
caber — é aritmética do orçamento, não item de lista. Registrar como pendência
criaria dívida falsa, que alguém tentaria pagar sem perceber que não há como.

Quem consultou sabe qual CNPJ digitou, então a perda é pequena e conhecida.
"""

from __future__ import annotations

import logging
from typing import Annotated, Final

from fastapi import APIRouter, Path

from grafo_societario.api.deps import AcervoDep
from grafo_societario.api.resolucao import analisar_ou_422, resolver
from grafo_societario.api.schemas import RespostaDeEmpresa
from grafo_societario.api.texto import milhar

logger = logging.getLogger(__name__)

DESCRICAO_DO_CNPJ: Final = (
    "CNPJ com os quatorze dígitos. **Num caminho de URL use só os dígitos** — a máscara tem "
    "uma barra, que precisaria vir codificada como `%2F`. O `cnpj_basico` de oito dígitos não "
    "é aceito: ele não tem verificador, e um erro de digitação viraria consulta silenciosa a "
    "outra empresa."
)

SEM_VINCULO: Final = (
    "Esta empresa existe no recorte e não tem vínculo societário nenhum registrado. É o caso de "
    "74,8% das empresas: no empresário individual o dono está na razão social, e o projeto "
    "recusou deliberadamente extraí-lo. A razão social não aparece aqui porque o artefato "
    "publicado carrega apenas os nós do grafo, e empresa sem vínculo não é nó — não é dado "
    "faltando."
)

roteador = APIRouter(tags=["consulta"])


@roteador.get(
    "/empresa/{cnpj}",
    summary="Atributos e contagens de uma empresa",
    response_model=RespostaDeEmpresa,
)
def consultar_empresa(
    acervo: AcervoDep,
    cnpj: Annotated[str, Path(description=DESCRICAO_DO_CNPJ, examples=["11222333000181"])],
) -> RespostaDeEmpresa:
    """A empresa em si: razão social, vínculos e componente. **Sem os vizinhos.**

    Quem quer os vizinhos chama `/vizinhanca` — ver o topo do módulo para o
    motivo de as duas rotas não se sobreporem.

    `404` fica reservado a CNPJ que não é nó nem está no recorte, e `422` a CNPJ
    malformado. Empresa sem vínculo **responde `200`**: ela existe.
    """
    analisado = analisar_ou_422(cnpj, "cnpj")
    (ponta,) = resolver(acervo, analisado)

    if ponta.indice is None:
        return RespostaDeEmpresa(
            cnpj=analisado.completo,
            tem_vinculo=False,
            explicacao=SEM_VINCULO,
            nome=None,
            no_recorte=True,
            vinculos_no_recorte=0,
            tamanho_do_componente_no_recorte=None,
        )

    vinculos = acervo.grafo.grau(ponta.indice)
    componente = acervo.tamanho_do_componente(ponta.indice)
    no_recorte = acervo.catalogo.no_recorte_de(ponta.indice)
    return RespostaDeEmpresa(
        cnpj=analisado.completo,
        tem_vinculo=True,
        explicacao=_explicar(vinculos, componente, no_recorte),
        nome=acervo.catalogo.nome_de(ponta.indice),
        no_recorte=no_recorte,
        vinculos_no_recorte=vinculos,
        tamanho_do_componente_no_recorte=componente,
    )


def _explicar(vinculos: int, componente: int, no_recorte: bool | None) -> str:
    quantos = "1 vínculo" if vinculos == 1 else f"{milhar(vinculos)} vínculos"
    quantos_no = "1 nó" if componente == 1 else f"{milhar(componente)} nós"
    frase = (
        f"Empresa com {quantos} no recorte, num componente conexo de {quantos_no}. Os dois "
        "números são piso e não total: só foram ingeridos sócios de empresas cuja matriz está "
        "no recorte."
    )
    if no_recorte is False:
        return (
            f"{frase} Esta empresa é um **conector**: a matriz dela está fora do recorte, e ela "
            "aparece no grafo por ser sócia de uma empresa daqui — os demais vínculos dela não "
            "foram ingeridos."
        )
    return frase
