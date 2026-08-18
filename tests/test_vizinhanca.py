"""As duas rotas que respondem sobre uma empresa só, e por que são duas.

`/empresa` e `/vizinhanca` **não se sobrepõem**, e o critério não é de estilo:
elas têm domínios diferentes. As 14,8 milhões de empresas do recorte que não têm
vínculo nenhum não são nós do grafo — `/empresa` responde sobre elas, e
`/vizinhanca` não teria o que devolver. Rotas com domínios diferentes não são a
mesma rota com outro nome, e é por isso que uma não devolve os vizinhos da outra.
"""

from __future__ import annotations

from typing import Any

import pytest
from fastapi.testclient import TestClient

from grafo_societario.api.cnpj import formatar
from grafo_societario.api.main import criar_aplicacao
from grafo_societario.api.vizinhanca import SALTOS_PADRAO, TETO_DE_NOS_PADRAO
from grafo_societario.config import Config
from test_caminho import (  # noqa: F401
    ALFA,
    BRAVO,
    CONECTOR,
    DESCONHECIDA,
    SOZINHA,
    grafo_de_exemplo,
)


@pytest.fixture
def cliente(grafo_de_exemplo: Config) -> Any:  # noqa: F811
    with TestClient(criar_aplicacao(grafo_de_exemplo)) as aberto:
        yield aberto


def digitos(cnpj_basico: str) -> str:
    """O CNPJ só com dígitos, que é a forma que cabe num caminho de URL."""
    return formatar(int(cnpj_basico)).replace(".", "").replace("/", "").replace("-", "")


def vizinhos_de(cliente: Any, cnpj_basico: str, **extras: Any) -> Any:
    return cliente.get("/vizinhanca", params={"cnpj": formatar(int(cnpj_basico)), **extras})


def empresa_de(cliente: Any, cnpj_basico: str) -> Any:
    return cliente.get(f"/empresa/{digitos(cnpj_basico)}")


# ------------------------------------------------------------------ /vizinhanca


def test_devolve_o_subgrafo_induzido_ate_dois_saltos(cliente: Any) -> None:
    """ALFA alcança FULANO e CONECTORA em um salto, e BRAVO em dois."""
    corpo = vizinhos_de(cliente, ALFA).json()

    assert corpo["tem_vinculo"] is True
    assert len(corpo["nos"]) == 4
    assert sorted(no["profundidade"] for no in corpo["nos"]) == [0, 1, 1, 2]
    assert corpo["saltos"] == 2
    assert corpo["truncada"] is False


def test_a_origem_vem_na_vizinhanca_com_profundidade_zero(cliente: Any) -> None:
    corpo = vizinhos_de(cliente, ALFA).json()

    origem = [no for no in corpo["nos"] if no["profundidade"] == 0]

    assert len(origem) == 1
    assert origem[0]["cnpj"] == formatar(int(ALFA))


def test_as_arestas_referenciam_posicoes_da_resposta(cliente: Any) -> None:
    """O índice denso do grafo **nunca sai na API**: ele é atribuído pela ordem do
    identificador e o conjunto de nós muda a cada competência.

    Uma aresta que carregasse o índice funcionaria hoje e apontaria para outra
    empresa no mês seguinte, sem erro e com aparência de acerto. Por isso as
    arestas referenciam posições nesta resposta, que morrem com ela.
    """
    corpo = vizinhos_de(cliente, ALFA).json()
    quantos = len(corpo["nos"])

    assert corpo["arestas"], "a fixture tem arestas; sem elas o teste não afirma nada"
    for de, para in corpo["arestas"]:
        assert 0 <= de < quantos
        assert 0 <= para < quantos
        assert de < para, "o menor à esquerda, e cada aresta uma vez só"


def test_o_teto_recusa_o_nivel_inteiro_e_diz_de_que_tamanho_ele_era(cliente: Any) -> None:
    """Meio nível entregaria um subgrafo que parece completo e não é.

    O número do nível recusado é informação por si só: diz o tamanho do que não
    está sendo visto, e é o que permite a quem consultou pedir um teto maior
    sabendo quanto pedir.
    """
    corpo = vizinhos_de(cliente, ALFA, teto_de_nos=1).json()

    assert corpo["truncada"] is True
    assert corpo["nivel_recusado"] == 2
    assert len(corpo["nos"]) == 1
    assert corpo["saltos"] == 0


def test_empresa_sem_vinculo_responde_e_nao_da_404(cliente: Any) -> None:
    """Ela existe no recorte. `404` diria que não existe, o que é falso sobre
    74,8% das empresas."""
    resposta = vizinhos_de(cliente, SOZINHA)

    assert resposta.status_code == 200
    corpo = resposta.json()
    assert corpo["tem_vinculo"] is False
    assert corpo["nos"] == []
    assert corpo["arestas"] == []
    assert corpo["explicacao"]


def test_nome_de_pessoa_fisica_nao_vaza_na_vizinhanca(cliente: Any) -> None:
    pessoas = [
        no for no in vizinhos_de(cliente, ALFA).json()["nos"] if no["tipo"] != "pessoa_juridica"
    ]

    assert pessoas, "a fixture tem pessoa física; sem ela o teste não afirma nada"
    assert all(no["nome"] is None for no in pessoas)


def test_vizinhanca_de_cnpj_desconhecido_e_404(cliente: Any) -> None:
    assert vizinhos_de(cliente, DESCONHECIDA).status_code == 404


def test_vizinhanca_de_cnpj_malformado_e_422(cliente: Any) -> None:
    assert cliente.get("/vizinhanca", params={"cnpj": "11111111"}).status_code == 422


def test_os_padroes_sao_dois_saltos_e_mil_nos(cliente: Any) -> None:
    assert (SALTOS_PADRAO, TETO_DE_NOS_PADRAO) == (2, 1000)

    corpo = vizinhos_de(cliente, ALFA).json()

    assert corpo["saltos_pedidos"] == 2
    assert corpo["teto_de_nos"] == 1000


def test_a_descricao_do_teto_traz_os_dois_regimes(cliente: Any) -> None:
    """De nó comum a bola cresce devagar; de hub ela estoura no primeiro salto.

    São três ordens de grandeza, e quem escolhe o teto sem esse número escolhe no
    escuro — a mediana de um regime esconde o outro inteiro.
    """
    parametros = cliente.get("/openapi.json").json()["paths"]["/vizinhanca"]["get"]["parameters"]
    teto = next(p for p in parametros if p["name"] == "teto_de_nos")

    # Mediana e p95 dos dois regimes com este padrão aplicado, mais o tamanho do
    # primeiro salto de um hub sem teto — que é o motivo de o teto existir.
    for numero in ("3 nós", "17", "747", "991", "1.132"):
        assert numero in teto["description"], f"a descrição precisa citar {numero}"


# --------------------------------------------------------------------- /empresa


def test_empresa_devolve_atributos_e_contagens(cliente: Any) -> None:
    corpo = empresa_de(cliente, ALFA).json()

    assert corpo["cnpj"] == formatar(int(ALFA))
    assert corpo["nome"] == "ALFA COMERCIO LTDA"
    assert corpo["tem_vinculo"] is True
    assert corpo["vinculos_no_recorte"] == 2
    assert corpo["tamanho_do_componente_no_recorte"] == 4


def test_empresa_nao_devolve_vizinhos(cliente: Any) -> None:
    """Se ela devolvesse, viraria `/vizinhanca?saltos=1` com outro nome — e duas
    rotas que devolvem a mesma coisa divergem no primeiro commit que mexe numa
    só, sem nada falhar."""
    corpo = empresa_de(cliente, ALFA).json()

    assert "nos" not in corpo
    assert "arestas" not in corpo
    assert "vizinhos" not in corpo


def test_empresa_sem_vinculo_explica_por_que_nao_ha_nome(cliente: Any) -> None:
    """Nome nulo sem explicação é a única coisa nesta resposta que um usuário
    leria como defeito — e ela descreve 74,8% do recorte, o caso majoritário.

    O motivo é do artefato: ele carrega apenas os nós do grafo, e empresa sem
    vínculo não é nó.
    """
    corpo = empresa_de(cliente, SOZINHA).json()

    assert corpo["tem_vinculo"] is False
    assert corpo["nome"] is None
    assert corpo["vinculos_no_recorte"] == 0
    assert corpo["tamanho_do_componente_no_recorte"] is None
    assert "razão social" in corpo["explicacao"]
    assert "não é nó" in corpo["explicacao"] or "nós do grafo" in corpo["explicacao"]


def test_empresa_conector_diz_que_esta_fora_do_recorte(cliente: Any) -> None:
    corpo = empresa_de(cliente, CONECTOR).json()

    assert corpo["tem_vinculo"] is True
    assert corpo["no_recorte"] is False
    assert corpo["nome"] == "CONECTORA HOLDING LTDA"


def test_empresa_desconhecida_e_404(cliente: Any) -> None:
    assert empresa_de(cliente, DESCONHECIDA).status_code == 404


def test_empresa_com_cnpj_malformado_e_422(cliente: Any) -> None:
    assert cliente.get("/empresa/11111111").status_code == 422


def test_o_campo_do_componente_declara_que_e_no_recorte(cliente: Any) -> None:
    """Mesma disciplina de `vinculos_no_recorte`: é componente **dentro do
    recorte**, e o nome do campo é a última chance de a ressalva chegar."""
    esquema = cliente.get("/openapi.json").json()["components"]["schemas"]["RespostaDeEmpresa"]

    assert "tamanho_do_componente_no_recorte" in esquema["properties"]
    assert "componente" not in [
        nome for nome in esquema["properties"] if nome == "tamanho_do_componente"
    ]
