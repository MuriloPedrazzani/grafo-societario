"""A pseudonimização na resposta, e os três testes que provam que ela existe.

O teste central deste arquivo serve um artefato **construído com os nomes** por
uma API com `EXPOR_PF=false`, e exige que a resposta saia sem eles. Testar
pseudonimização contra um artefato que já não tem nome prova apenas que não se
pode devolver o que não existe — e é assim que uma proteção some sem nenhum
teste ficar vermelho.

O segundo prova a propriedade que o **identificador de pessoa física não tinha**,
e que o derrubou do artefato: o rótulo é função da **posição na resposta**, e não
da identidade do nó. Se ele voltar a derivar da identidade, correlacionar duas
consultas volta a ser possível, e é exatamente esse vetor que foi fechado.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient

from grafo_societario.api.cnpj import formatar
from grafo_societario.api.main import criar_aplicacao
from grafo_societario.config import Config
from grafo_societario.graph.build import gerar_arestas, gerar_nos, serializar_csr
from grafo_societario.graph.components import calcular_componentes
from grafo_societario.graph.metadados import serializar_metadados
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

ALFA = "11111111"
BRAVO = "22222222"
CHARLIE = "33333333"

PRIMEIRA = "PRIMEIRA PESSOA DA CADEIA"
SEGUNDA = "SEGUNDA PESSOA DA CADEIA"


def construir(raiz: Path, *, expor_pf: bool) -> Config:
    """Uma cadeia ALFA — PRIMEIRA — BRAVO — SEGUNDA — CHARLIE, de 4 saltos.

    A cadeia existe para que a **mesma** pessoa ocupe posições diferentes em
    consultas diferentes: SEGUNDA é o índice 3 do caminho ALFA→CHARLIE e o índice
    1 do caminho BRAVO→CHARLIE.
    """
    config = Config(competencia="2026-06", data_dir=raiz, uf_alvo="SP", expor_pf=expor_pf)
    bases = (ALFA, BRAVO, CHARLIE)
    gravar_estabelecimentos(config, [estabelecimento(cnpj) for cnpj in bases])
    aplicar_recorte_por_uf(config)
    gravar_empresas(
        config,
        [
            empresa(ALFA, razao_social="ALFA COMERCIO LTDA"),
            empresa(BRAVO, razao_social="BRAVO SERVICOS SA"),
            empresa(CHARLIE, razao_social="CHARLIE LOGISTICA ME"),
        ],
    )
    _gravar_dominio(config, "Naturezas", NATUREZAS_PADRAO)
    _gravar_dominio(config, "Qualificacoes", QUALIFICACOES_PADRAO)
    _gravar_dominio(config, "Paises", PAISES_PADRAO)
    tipar_empresas(config)
    gravar_socios(
        config,
        [
            socio(ALFA, nome=PRIMEIRA, documento="***111118**"),
            socio(BRAVO, nome=PRIMEIRA, documento="***111118**"),
            socio(BRAVO, nome=SEGUNDA, documento="***222228**"),
            socio(CHARLIE, nome=SEGUNDA, documento="***222228**"),
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


@pytest.fixture(scope="module")
def com_nomes(tmp_path_factory: pytest.TempPathFactory) -> Config:
    """Artefato construído com `EXPOR_PF=true`: os nomes de PF **estão** nele."""
    return construir(tmp_path_factory.mktemp("com_nomes"), expor_pf=True)


@pytest.fixture(scope="module")
def sem_nomes(tmp_path_factory: pytest.TempPathFactory) -> Config:
    """Artefato construído com o padrão: os nomes de PF não entraram."""
    return construir(tmp_path_factory.mktemp("sem_nomes"), expor_pf=False)


def consultar(config: Config, de: str, para: str) -> Any:
    with TestClient(criar_aplicacao(config)) as cliente:
        return cliente.get(
            "/caminho",
            params={
                "de": formatar(int(de)),
                "para": formatar(int(para)),
                "profundidade_maxima": 10,
            },
        ).json()


def pessoas(corpo: Any) -> list[Any]:
    return [no for no in corpo["caminho"] if no["tipo"] == "pessoa_fisica"]


# ------------------------------------- 1. a combinação perigosa: artefato com nome


def test_a_fixture_com_expor_pf_realmente_guarda_o_nome(com_nomes: Config) -> None:
    """Controle positivo da fixture, sem o qual o teste seguinte não prova nada.

    Se o artefato não tivesse os nomes, "a API não devolveu nome" seria verdade
    por vacuidade — e é essa vacuidade que faz uma proteção sumir sem nenhum
    teste ficar vermelho.
    """
    from grafo_societario.graph.catalogo import abrir_catalogo

    cat = abrir_catalogo(com_nomes)

    nomes = {cat.nome_de(no) for no in range(cat.nos)}

    assert PRIMEIRA in nomes, "a construção com EXPOR_PF=true precisa gravar o nome"
    assert SEGUNDA in nomes


def test_api_sem_expor_pf_nao_devolve_nome_que_o_artefato_tem(com_nomes: Config) -> None:
    """**O teste que prova que a API faz alguma coisa.**

    Artefato com os nomes, servido por uma aplicação com `EXPOR_PF=false`: a
    resposta sai sem eles. Sem esta combinação, a pseudonimização da borda poderia
    ser removida inteira e a suíte continuaria verde, porque o artefato de teste
    já não teria nome nenhum a vazar.
    """
    servida = com_nomes.model_copy(update={"expor_pf": False})

    corpo = consultar(servida, ALFA, CHARLIE)

    inteiro = str(corpo)
    assert PRIMEIRA not in inteiro
    assert SEGUNDA not in inteiro
    assert all(no["nome"] is None for no in pessoas(corpo))
    assert all(no["rotulo"] for no in pessoas(corpo))


def test_a_razao_social_de_empresa_nao_e_pseudonimizada(com_nomes: Config) -> None:
    """A flag cobre pessoa, não empresa. Razão social de PJ é pública."""
    servida = com_nomes.model_copy(update={"expor_pf": False})

    corpo = consultar(servida, ALFA, CHARLIE)

    empresas = [no for no in corpo["caminho"] if no["tipo"] == "pessoa_juridica"]
    assert [no["nome"] for no in empresas] == [
        "ALFA COMERCIO LTDA",
        "BRAVO SERVICOS SA",
        "CHARLIE LOGISTICA ME",
    ]


# ------------------------------------- 2. o rótulo não pode correlacionar


def test_o_mesmo_no_em_posicoes_diferentes_recebe_rotulos_diferentes(sem_nomes: Config) -> None:
    """A propriedade que o identificador de PF **não tinha**, e que o derrubou.

    Aquele identificador era `sha256("pessoa_fisica|" + nome + "|" + cpf)`, igual
    em toda consulta — e portanto uma chave para correlacionar respostas e
    remontar a pessoa. O rótulo é função da **posição na resposta**: a mesma
    pessoa, consultada por outro par, recebe outro rótulo.

    Se este teste quebrar, alguém derivou o rótulo da identidade do nó, e o vetor
    que foi fechado está reaberto.
    """
    longo = consultar(sem_nomes, ALFA, CHARLIE)["caminho"]
    curto = consultar(sem_nomes, BRAVO, CHARLIE)["caminho"]

    # A mesma pessoa: vizinha de CHARLIE nos dois caminhos, em posições distintas.
    no_longo = longo[3]
    no_curto = curto[1]

    assert no_longo["tipo"] == no_curto["tipo"] == "pessoa_fisica"
    assert no_longo["rotulo"] != no_curto["rotulo"], (
        "o mesmo nó em posições diferentes manteve o rótulo: ele está derivando da "
        "identidade, e correlacionar duas consultas voltou a ser possível"
    )


def test_o_rotulo_e_a_posicao_e_nada_mais(sem_nomes: Config) -> None:
    """Dentro de uma resposta o rótulo é único e corresponde à posição.

    É o que permite a `arestas` referenciar posições e a interface juntar as duas
    listas sem ambiguidade: duas referências ao mesmo nó caem na mesma posição, e
    portanto no mesmo rótulo.
    """
    caminho = consultar(sem_nomes, ALFA, CHARLIE)["caminho"]

    rotulos = [no["rotulo"] for no in caminho if no["rotulo"] is not None]

    assert len(rotulos) == len(set(rotulos)), "dois nós da mesma resposta com o mesmo rótulo"
    assert caminho[1]["rotulo"] == "Sócio 2"
    assert caminho[3]["rotulo"] == "Sócio 4"


def test_a_mesma_consulta_devolve_sempre_os_mesmos_rotulos(sem_nomes: Config) -> None:
    """Determinismo vale aqui como vale no artefato."""
    primeira = consultar(sem_nomes, ALFA, CHARLIE)["caminho"]
    segunda = consultar(sem_nomes, ALFA, CHARLIE)["caminho"]

    assert [no["rotulo"] for no in primeira] == [no["rotulo"] for no in segunda]


def test_empresa_nao_recebe_rotulo(sem_nomes: Config) -> None:
    """Pessoa jurídica tem razão social pública; o rótulo existe para quem não tem."""
    caminho = consultar(sem_nomes, ALFA, CHARLIE)["caminho"]

    for no in caminho:
        if no["tipo"] == "pessoa_juridica":
            assert no["rotulo"] is None


# ------------------------------------- 3. o caminho com EXPOR_PF=true


def test_api_com_expor_pf_devolve_o_nome(com_nomes: Config) -> None:
    """Sem este caminho a flag não está decidindo nada.

    É a mesma regra que o artefato aplica do lado da construção: uma opção que
    só tem um comportamento observado não é uma opção, é uma constante com nome
    de opção.
    """
    corpo = consultar(com_nomes, ALFA, CHARLIE)

    assert [no["nome"] for no in pessoas(corpo)] == [PRIMEIRA, SEGUNDA]


def test_a_forma_da_resposta_nao_muda_entre_os_dois_modos(com_nomes: Config) -> None:
    """Cliente que quebra ao trocar a flag é cliente que teria quebrado em produção.

    O `rotulo` continua presente com os nomes expostos, e o `nome` continua no
    esquema com eles ocultos: o que muda é o valor, nunca as chaves.
    """
    exposta = consultar(com_nomes, ALFA, CHARLIE)
    oculta = consultar(com_nomes.model_copy(update={"expor_pf": False}), ALFA, CHARLIE)

    assert [set(no) for no in exposta["caminho"]] == [set(no) for no in oculta["caminho"]]
    assert [no["rotulo"] for no in exposta["caminho"]] == [no["rotulo"] for no in oculta["caminho"]]
    assert all(no["nome"] is not None for no in pessoas(exposta))
    assert all(no["nome"] is None for no in pessoas(oculta))


def test_o_health_declara_em_qual_modo_a_aplicacao_esta(com_nomes: Config) -> None:
    """Quem opera precisa saber se a instância que está no ar expõe nome."""
    with TestClient(criar_aplicacao(com_nomes)) as cliente:
        assert cliente.get("/health").json()["expor_pf"] is True

    servida = com_nomes.model_copy(update={"expor_pf": False})
    with TestClient(criar_aplicacao(servida)) as cliente:
        assert cliente.get("/health").json()["expor_pf"] is False
