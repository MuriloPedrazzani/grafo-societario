"""O endpoint de caminho, e a regra de que ele nunca afirma o que não sabe.

Os testes centrais aqui não são os do caminho encontrado — são os dos **quatro
outros desfechos**. Três deles significam coisas diferentes e dois deles não
afirmam ausência nenhuma, e o defeito que esta fase existe para não cometer é
colapsá-los em "não há vínculo" na borda, desfazendo dentro de uma linha de
serialização todo o cuidado da Fase 5.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient

from grafo_societario.api.caminho import PROFUNDIDADE_PADRAO, descrever
from grafo_societario.api.cnpj import formatar
from grafo_societario.api.main import criar_aplicacao
from grafo_societario.api.schemas import DesfechoDaConsulta
from grafo_societario.config import Config
from grafo_societario.graph.build import gerar_arestas, gerar_nos, serializar_csr
from grafo_societario.graph.components import calcular_componentes
from grafo_societario.graph.metadados import serializar_metadados
from grafo_societario.graph.traversal import Caminho, Desfecho
from grafo_societario.transform.identity import gerar_identidades
from grafo_societario.transform.silver import (
    aplicar_recorte_por_uf,
    tipar_empresas,
    tipar_socios,
)
from test_silver import (
    NATUREZAS_PADRAO,
    PAISES_PADRAO,
    QUALIFICACOES_PADRAO,
    _gravar_dominio,
    empresa,
    estabelecimento,
    gravar_empresas,
    gravar_estabelecimentos,
    gravar_socios,
    socio,
)

# As bases da fixture. Todas sintéticas, e o CNPJ completo sai do cálculo do
# verificador — o mesmo caminho que a resposta usa.
ALFA = "11111111"
BRAVO = "22222222"
SOZINHA = "33333333"
DELTA = "44444444"
ECHO = "55555555"
CONECTOR = "99999999"
DESCONHECIDA = "66666666"


@pytest.fixture
def grafo_de_exemplo(tmp_path: Path) -> Config:
    """Cinco empresas em duas ilhas, mais uma sem vínculo nenhum.

    | | |
    |---|---|
    | ALFA — FULANO — BRAVO | caminho de 2 saltos, com pessoa física no meio |
    | DELTA — BELTRANO — ECHO | outra ilha: componente diferente do primeiro |
    | SOZINHA | no recorte, sem sócio, fora do CSR — o `sem_vinculo` |
    | CONECTOR | nó do grafo e **fora** do recorte: sócia de ALFA, matriz noutra UF |
    | DESCONHECIDA | não é nó nem está no recorte — o `404` |
    """
    config = Config(competencia="2026-06", data_dir=tmp_path, uf_alvo="SP")
    gravar_estabelecimentos(
        config,
        [estabelecimento(cnpj) for cnpj in (ALFA, BRAVO, SOZINHA, DELTA, ECHO)],
    )
    aplicar_recorte_por_uf(config)
    gravar_empresas(
        config,
        [
            empresa(ALFA, razao_social="ALFA COMERCIO LTDA"),
            empresa(BRAVO, razao_social="BRAVO SERVICOS SA"),
            empresa(SOZINHA, razao_social="CHARLIE SOZINHA ME"),
            empresa(DELTA, razao_social="DELTA PARTICIPACOES LTDA"),
            empresa(ECHO, razao_social="ECHO LOGISTICA LTDA"),
        ],
    )
    _gravar_dominio(config, "Naturezas", NATUREZAS_PADRAO)
    _gravar_dominio(config, "Qualificacoes", QUALIFICACOES_PADRAO)
    _gravar_dominio(config, "Paises", PAISES_PADRAO)
    tipar_empresas(config)
    gravar_socios(
        config,
        [
            socio(ALFA, nome="FULANO DE TAL", documento="***123458**"),
            socio(BRAVO, nome="FULANO DE TAL", documento="***123458**"),
            socio(DELTA, nome="BELTRANO DE TAL", documento="***777779**"),
            socio(ECHO, nome="BELTRANO DE TAL", documento="***777779**"),
            # O conector: pessoa jurídica de fora do recorte, sócia de ALFA. É nó
            # do grafo e não está no `existencia.npy` — são 36.810 no dado real.
            socio(ALFA, tipo="1", nome="CONECTORA HOLDING LTDA", documento=CONECTOR + "000191"),
        ],
    )
    tipar_socios(config)
    gerar_identidades(config)
    gerar_nos(config)
    gerar_arestas(config)
    serializar_csr(config)
    calcular_componentes(config)
    serializar_metadados(config)
    return config


@pytest.fixture
def cliente(grafo_de_exemplo: Config) -> Any:
    with TestClient(criar_aplicacao(grafo_de_exemplo)) as aberto:
        yield aberto


def consultar(cliente: Any, de: str, para: str, **extras: Any) -> Any:
    return cliente.get(
        "/caminho", params={"de": formatar(int(de)), "para": formatar(int(para)), **extras}
    )


# ---------------------------------------------------------------- o caminho existe


def test_encontra_o_caminho_com_a_pessoa_fisica_no_meio(cliente: Any) -> None:
    resposta = consultar(cliente, ALFA, BRAVO)

    assert resposta.status_code == 200
    corpo = resposta.json()
    assert corpo["desfecho"] == "encontrado"
    assert corpo["afirma_ausencia"] is False
    assert corpo["distancia"] == 2
    assert [no["tipo"] for no in corpo["caminho"]] == [
        "pessoa_juridica",
        "pessoa_fisica",
        "pessoa_juridica",
    ]


def test_a_pessoa_fisica_do_caminho_nao_traz_nome(cliente: Any) -> None:
    """A pseudonimização decide na geração, e o artefato já não tem o nome.

    O rótulo local à resposta é do commit 36; aqui o que precisa valer é que
    nome nenhum de pessoa física atravessa.
    """
    meio = consultar(cliente, ALFA, BRAVO).json()["caminho"][1]

    assert meio["tipo"] == "pessoa_fisica"
    assert meio["nome"] is None
    assert meio["cnpj"] is None


def test_o_no_declara_vinculos_no_recorte_e_nao_grau(cliente: Any) -> None:
    """O nome do campo é a última chance de a ressalva chegar ao leitor.

    O commit 19 nomeou a coluna `vinculos_no_recorte`, e não `grau`, de propósito,
    e o README tem uma seção sobre isso. Um JSON com `"grau": 2` se lê como "tem
    2 sócios" — e o número é 2 **dentro do recorte da UF alvo**. Quem participa de
    3 empresas em SP e 40 no Rio aparece com 3.

    Carregar essa distinção por três fases e perdê-la na serialização seria
    desfazer o cuidado no último metro.
    """
    no = consultar(cliente, ALFA, BRAVO).json()["caminho"][0]

    assert "grau" not in no
    assert no["vinculos_no_recorte"] == 2


def test_a_descricao_do_campo_diz_que_o_numero_e_piso(cliente: Any) -> None:
    """A ressalva vive na documentação da rota, e não só no nome do campo."""
    esquema = cliente.get("/openapi.json").json()["components"]["schemas"]["NoDaResposta"]

    descricao = esquema["properties"]["vinculos_no_recorte"]["description"]

    assert "piso" in descricao
    assert "recorte" in descricao


def test_a_taxa_de_colisao_viaja_com_o_no_de_pessoa_fisica(cliente: Any) -> None:
    """E é nula em quem não é fundido por máscara, com `confianca` dizendo o motivo."""
    caminho = consultar(cliente, ALFA, BRAVO).json()["caminho"]
    pessoa, empresa = caminho[1], caminho[0]

    assert pessoa["confianca"] == "estimada"
    assert isinstance(pessoa["taxa_de_colisao"], float)
    assert empresa["confianca"] == "exata"
    assert empresa["taxa_de_colisao"] is None


def test_devolve_o_cnpj_completo_das_empresas(cliente: Any) -> None:
    pontas = consultar(cliente, ALFA, BRAVO).json()["caminho"]

    assert pontas[0]["cnpj"] == "11.111.111/0001-91"
    assert pontas[2]["cnpj"] == "22.222.222/0001-91"


def test_o_cnpj_devolvido_volta_a_ser_aceito(cliente: Any) -> None:
    """Ida e volta pela borda: quem segue um caminho copia o CNPJ de um salto
    para consultar o próximo, e o serviço não pode recusar o que acabou de emitir."""
    devolvido = consultar(cliente, ALFA, BRAVO).json()["caminho"][0]["cnpj"]

    resposta = cliente.get("/caminho", params={"de": devolvido, "para": formatar(int(BRAVO))})

    assert resposta.status_code == 200
    assert resposta.json()["desfecho"] == "encontrado"


def test_o_caminho_e_simetrico(cliente: Any) -> None:
    ida = consultar(cliente, ALFA, BRAVO).json()["caminho"]
    volta = consultar(cliente, BRAVO, ALFA).json()["caminho"]

    assert [no["cnpj"] for no in volta] == [no["cnpj"] for no in reversed(ida)]


def test_a_empresa_consigo_mesma_tem_zero_saltos(cliente: Any) -> None:
    corpo = consultar(cliente, ALFA, ALFA).json()

    assert corpo["desfecho"] == "encontrado"
    assert corpo["distancia"] == 0
    assert len(corpo["caminho"]) == 1


# ------------------------------------------- os quatro desfechos que não são "achei"


def test_empresa_sem_vinculo_nao_vira_componentes_diferentes(cliente: Any) -> None:
    """`sem_vinculo` descreve 74,8% do recorte, e é o custo ético medido do projeto.

    Ela não pode virar `componentes_diferentes`: as duas afirmam ausência, mas só
    a segunda diz que a empresa tem vínculos e eles não chegam à outra.
    """
    corpo = consultar(cliente, SOZINHA, ALFA).json()

    assert corpo["desfecho"] == "sem_vinculo"
    assert corpo["afirma_ausencia"] is True
    assert corpo["caminho"] == []
    assert corpo["distancia"] is None


def test_empresa_sem_vinculo_consigo_mesma_continua_sem_vinculo(cliente: Any) -> None:
    """Não é `encontrado` de zero saltos: ela não é nó do grafo, e responder que
    há caminho de uma empresa para si mesma diria que ela tem vínculo."""
    assert consultar(cliente, SOZINHA, SOZINHA).json()["desfecho"] == "sem_vinculo"


def test_componentes_diferentes_quando_as_duas_tem_vinculo(cliente: Any) -> None:
    corpo = consultar(cliente, ALFA, DELTA).json()

    assert corpo["desfecho"] == "componentes_diferentes"
    assert corpo["afirma_ausencia"] is True


def test_alem_do_limite_traz_a_distancia_real(cliente: Any) -> None:
    """O limite governa **o que é mostrado**, não até onde se procura.

    A busca vai até o fim com o orçamento como único freio, então este desfecho
    deixou de ser "não procurei até lá" e passou a ser um achado verdadeiro: o
    vínculo existe e está a esta distância. É a informação que o projeto vem
    dizendo ser a interessante — grafo societário não é mundo pequeno.
    """
    corpo = consultar(cliente, ALFA, BRAVO, profundidade_maxima=1).json()

    assert corpo["desfecho"] == "alem_do_limite"
    assert corpo["afirma_ausencia"] is False
    assert corpo["distancia"] == 2, "a distância real, e não o limite pedido"
    assert "2 saltos" in corpo["explicacao"]


def test_alem_do_limite_nao_mostra_o_caminho(cliente: Any) -> None:
    """Saber a distância é diferente de ver o caminho, e o limite separa os dois."""
    corpo = consultar(cliente, ALFA, BRAVO, profundidade_maxima=1).json()

    assert corpo["caminho"] == []
    assert corpo["profundidade_maxima"] == 1


def test_o_limite_deixa_de_esconder_a_existencia_do_vinculo(cliente: Any) -> None:
    """Controle: o mesmo par, com limite generoso, dá a mesma distância.

    Se a busca ainda parasse no limite, a distância mudaria com ele — e é
    exatamente isso que este teste existe para impedir que volte.
    """
    curto = consultar(cliente, ALFA, BRAVO, profundidade_maxima=1).json()
    longo = consultar(cliente, ALFA, BRAVO, profundidade_maxima=50).json()

    assert curto["distancia"] == longo["distancia"] == 2
    assert curto["desfecho"] == "alem_do_limite"
    assert longo["desfecho"] == "encontrado"


def test_orcamento_excedido_nao_afirma_ausencia(
    cliente: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Este desfecho precisa de um grafo de centenas de milhares de nós para sair
    naturalmente, e o que se testa aqui é a borda, não a travessia.

    Por isso a busca é substituída — é o único ponto do arquivo em que isso
    acontece, e é para exercitar o caminho de resposta de um desfecho que a
    fixture não consegue produzir.
    """
    monkeypatch.setattr(
        "grafo_societario.api.caminho.buscar_caminho",
        lambda *args, **kwargs: Caminho((), Desfecho.ORCAMENTO_EXCEDIDO, 250_001),
    )

    corpo = consultar(cliente, ALFA, BRAVO).json()

    assert corpo["desfecho"] == "orcamento_excedido"
    assert corpo["afirma_ausencia"] is False
    assert corpo["visitados"] == 250_001


# --------------------------------------------- a precedência entre 404 e sem_vinculo


def test_cnpj_que_nao_e_no_nem_esta_no_recorte_e_404(cliente: Any) -> None:
    resposta = consultar(cliente, DESCONHECIDA, ALFA)

    assert resposta.status_code == 404
    assert formatar(int(DESCONHECIDA)) in resposta.json()["detail"]


def test_o_conector_de_fora_do_recorte_responde(cliente: Any) -> None:
    """`404` é "não conheço esta empresa", e **não** "fora do recorte".

    O conector é pessoa jurídica de outra UF que entrou no grafo por ser sócia de
    uma empresa daqui: é nó, tem aresta, e não está no `existencia.npy`. São
    36.810 no dado real. Recusá-lo faria a rota emitir o CNPJ dele dentro de um
    caminho e rejeitar o mesmo CNPJ na requisição seguinte — foi assim que o
    defeito apareceu, com um nó sorteado do maior componente devolvendo 404.
    """
    corpo = consultar(cliente, CONECTOR, BRAVO).json()

    assert corpo["desfecho"] == "encontrado"
    assert corpo["caminho"][0]["no_recorte"] is False
    assert corpo["caminho"][0]["nome"] == "CONECTORA HOLDING LTDA"


def test_o_conector_aparece_no_caminho_e_e_aceito_de_volta(cliente: Any) -> None:
    """A propriedade que motivou a correção, afirmada de ponta a ponta."""
    caminho = consultar(cliente, CONECTOR, BRAVO).json()["caminho"]
    devolvido = caminho[0]["cnpj"]

    resposta = cliente.get("/caminho", params={"de": devolvido, "para": formatar(int(ALFA))})

    assert resposta.status_code == 200
    assert resposta.json()["desfecho"] == "encontrado"


@pytest.mark.parametrize("invertido", [False, True])
def test_o_404_vence_o_sem_vinculo_nos_dois_sentidos(cliente: Any, invertido: bool) -> None:
    """A ordem de checagem é decisão, e ela precisa valer nos dois sentidos.

    `sem_vinculo` significa "a empresa **existe** no recorte e não tem vínculo".
    Emiti-lo sobre um par em que a outra ponta não existe afirmaria a existência
    dela de lado, num campo que ninguém lê como afirmação. A requisição
    referencia algo que não existe, e é isso que a resposta tem de dizer.
    """
    de, para = (SOZINHA, DESCONHECIDA) if invertido else (DESCONHECIDA, SOZINHA)

    resposta = consultar(cliente, de, para)

    assert resposta.status_code == 404


def test_o_404_nomeia_as_duas_pontas_quando_as_duas_faltam(cliente: Any) -> None:
    resposta = cliente.get(
        "/caminho", params={"de": formatar(66666666), "para": formatar(77777777)}
    )

    detalhe = resposta.json()["detail"]
    assert resposta.status_code == 404
    assert formatar(66666666) in detalhe
    assert formatar(77777777) in detalhe


@pytest.mark.parametrize(
    "texto",
    ["11111111", "11.111.111/0001-90", "abc", "", "111111110001911"],
)
def test_cnpj_malformado_e_422(cliente: Any, texto: str) -> None:
    """Inclui o `cnpj_basico` de oito dígitos: ele é sintaticamente plausível e é
    exatamente o que não pode passar, porque não tem verificador."""
    resposta = cliente.get("/caminho", params={"de": texto, "para": formatar(int(BRAVO))})

    assert resposta.status_code == 422


# --------------------------------------- exaustividade: nenhum desfecho some em silêncio


@pytest.mark.parametrize("desfecho", list(Desfecho))
def test_todo_desfecho_da_travessia_chega_distinto_na_resposta(
    cliente: Any, monkeypatch: pytest.MonkeyPatch, desfecho: Desfecho
) -> None:
    """A companheira em tempo de execução do `assert_never`.

    O `match` exaustivo faz o mypy recusar o commit que acrescentar desfecho sem
    tratá-lo. Este teste cobre o outro lado: que os quatro que existem hoje saem
    distintos, e nenhum é mapeado para o vizinho mais parecido.
    """
    monkeypatch.setattr(
        "grafo_societario.api.caminho.buscar_caminho",
        lambda *args, **kwargs: Caminho((), desfecho, 7),
    )

    corpo = consultar(cliente, ALFA, BRAVO).json()

    assert corpo["desfecho"] == desfecho.value
    assert corpo["explicacao"]


AFIRMAM_AUSENCIA = {DesfechoDaConsulta.SEM_VINCULO, DesfechoDaConsulta.COMPONENTES_DIFERENTES}


@pytest.mark.parametrize("desfecho", list(DesfechoDaConsulta))
def test_todo_desfecho_da_consulta_declara_se_afirma_ausencia(desfecho: DesfechoDaConsulta) -> None:
    """Dois dos cinco afirmam ausência, e o campo existe para que o consumidor não
    precise saber quais de cabeça."""
    afirma, explicacao = descrever(desfecho, distancia=None, profundidade_maxima=10)

    assert explicacao.strip()
    assert afirma is (desfecho in AFIRMAM_AUSENCIA)


# ------------------------------------------------ o padrão da profundidade é declarado


def test_o_padrao_da_profundidade_e_dez(cliente: Any) -> None:
    assert PROFUNDIDADE_PADRAO == 10
    assert consultar(cliente, ALFA, BRAVO).json()["profundidade_maxima"] == 10


def test_a_documentacao_do_parametro_traz_a_distribuicao(cliente: Any) -> None:
    """Quem muda o valor precisa saber que a mediana é 20, senão vai achar que 5
    é generoso — a intuição dos seis graus vem de rede densa, e esta tem grau
    médio 2,79."""
    openapi = cliente.get("/openapi.json").json()
    parametros = openapi["paths"]["/caminho"]["get"]["parameters"]
    profundidade = next(p for p in parametros if p["name"] == "profundidade_maxima")

    descricao = profundidade["description"]
    for numero in ("20", "32", "38", "57"):
        assert numero in descricao, f"a distribuição precisa citar {numero}"
