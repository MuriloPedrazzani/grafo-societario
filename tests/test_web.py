"""A página servida pela própria API, e a guarda que atravessa a linguagem.

## O renderizador não tem regressão automatizada, e isso é escolha

Não há vitest, jest nem navegador headless neste projeto. Acrescentar um
ecossistema de JavaScript a um projeto Python, para uma página sem etapa de
build, seria ferramenta por ferramenta — e a regra deste repositório é que cada
componente justifique a própria existência.

O que torna a escolha defensável é onde a semântica mora. `desfecho`,
`afirma_ausencia` e `explicacao` são decididos e testados **no servidor**; o
JavaScript só escolhe título, tom e ação. Um renderizador sem regra de negócio
erra na aparência, e aparência se confere olhando.

**Mas alguém vai perguntar, então está dito:** os dez estados da tela são
verificados dirigindo o navegador à mão, e não por teste automatizado. O que
existe automatizado é a guarda abaixo.

## A guarda de fronteira

`test_a_pagina_conhece_todos_os_desfechos` lê o arquivo `.js` e exige que os
cinco valores de `DesfechoDaConsulta` apareçam nele. É comparação de string, é
feia, e é a única coisa entre "a API ganhou um desfecho" e "a página renderiza
sem tratamento para ele".

É o `assert_never` do commit 34 atravessando a borda da linguagem: o mypy garante
que o Python trata os cinco, e isto garante que a página os conhece. Sem ela, o
commit que acrescentar um desfecho passa verde e quebra a demonstração.
"""

from __future__ import annotations

import hashlib
import re
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient

from grafo_societario.api.main import criar_aplicacao
from grafo_societario.api.schemas import DesfechoDaConsulta
from grafo_societario.api.web import ESTATICOS, PAGINA
from grafo_societario.config import Config
from grafo_societario.graph.catalogo import TIPOS
from test_caminho import grafo_de_exemplo  # noqa: F401

APP_JS = ESTATICOS / "app.js"
DESENHO_JS = ESTATICOS / "desenho.js"
VENDORIZADO = ESTATICOS / "vendor" / "cytoscape.min.js"
PROCEDENCIA = Path(__file__).resolve().parents[1] / "docs" / "dependencias_vendorizadas.md"


@pytest.fixture
def cliente(grafo_de_exemplo: Config) -> Any:  # noqa: F811
    with TestClient(criar_aplicacao(grafo_de_exemplo)) as aberto:
        yield aberto


# ------------------------------------------- a fronteira entre Python e JavaScript


def test_a_pagina_conhece_todos_os_desfechos() -> None:
    """O `assert_never` do commit 34, atravessando a borda da linguagem.

    O mypy recusa o commit que acrescentar um desfecho sem tratá-lo no Python.
    Nada, do lado de cá, impediria a página de continuar com os cinco antigos —
    ela renderizaria o desfecho novo sem título, sem tom e sem ação, e a suíte
    ficaria verde.
    """
    fonte = APP_JS.read_text(encoding="utf-8")

    ausentes = [desfecho.value for desfecho in DesfechoDaConsulta if desfecho.value not in fonte]

    assert not ausentes, (
        f"a página não conhece {', '.join(ausentes)}. A API ganhou desfecho novo e o "
        f"renderizador não foi atualizado — {APP_JS.name} precisa tratá-lo."
    )


def test_o_desenho_conhece_todos_os_tipos_de_no() -> None:
    """A mesma guarda, para os três tipos: forma distinta por tipo é promessa da
    legenda, e um tipo novo sairia com a forma padrão sem nada falhar."""
    fonte = DESENHO_JS.read_text(encoding="utf-8")

    ausentes = [tipo for tipo in TIPOS if tipo not in fonte]

    assert not ausentes, f"o desenho não distingue {', '.join(ausentes)}"


def test_a_guarda_de_fronteira_sabe_reprovar() -> None:
    """Controle positivo: uma guarda que só compara strings passa fácil demais.

    Se ela não reprovasse um desfecho ausente, seria uma linha verde permanente
    dando a impressão de cobertura.
    """
    fonte = APP_JS.read_text(encoding="utf-8")

    assert "desfecho_que_nao_existe" not in fonte


# ------------------------------------------------------- a página é servida daqui


def test_a_raiz_devolve_a_pagina(cliente: Any) -> None:
    resposta = cliente.get("/")

    assert resposta.status_code == 200
    assert resposta.headers["content-type"].startswith("text/html")
    assert "Grafo Societário" in resposta.text


@pytest.mark.parametrize(
    "arquivo", ["app.js", "desenho.js", "estilo.css", "vendor/cytoscape.min.js"]
)
def test_os_estaticos_sao_servidos_pela_mesma_origem(cliente: Any, arquivo: str) -> None:
    """Mesma origem é o que dispensa CORS e paga um despertar em vez de dois.

    A biblioteca de desenho entra nisso: vinda de CDN, ela reintroduziria a
    segunda origem que a decisão evita, e com um modo de falha a mais — a
    demonstração cairia quando a CDN caísse, por motivo alheio a este projeto.
    """
    assert cliente.get(f"/static/{arquivo}").status_code == 200


def test_a_biblioteca_vendorizada_confere_com_a_procedencia() -> None:
    """Dependência sem procedência é dependência que ninguém confere depois.

    A soma está em `docs/dependencias_vendorizadas.md`, e este teste é o que
    impede a tabela de envelhecer em silêncio quando alguém trocar o arquivo.
    """
    soma = hashlib.sha256(VENDORIZADO.read_bytes()).hexdigest()
    documentado = PROCEDENCIA.read_text(encoding="utf-8")

    assert soma in documentado, (
        f"o cytoscape.min.js tem soma {soma}, que não está em "
        "docs/dependencias_vendorizadas.md. Trocar a biblioteca é decisão, e decisão "
        "atualiza a procedência junto."
    )


def bloco_dos_exemplos() -> str:
    fonte = APP_JS.read_text(encoding="utf-8")
    inicio = fonte.index("const EXEMPLOS = [")
    return fonte[inicio : fonte.index("\n];", inicio)]


def test_os_exemplos_guardam_pergunta_e_nunca_resposta() -> None:
    """Congelar o resultado faria o exemplo testar a si mesmo, e a página
    continuaria bonita com a API quebrada — a regra vem da curadoria da 6-G."""
    chaves = set(re.findall(r"^\s{4}(\w+):", bloco_dos_exemplos(), re.MULTILINE))

    assert chaves == {"rotulo", "modo", "de", "para", "profundidade"}, (
        f"os exemplos guardam {sorted(chaves)}. Só entram pergunta e rótulo: desfecho, "
        "distância ou qualquer pedaço da resposta faria o exemplo se autoconfirmar."
    )


def test_a_pagina_abre_com_um_exemplo_preenchido() -> None:
    """Formulário vazio é página morta: o visitante não tem CNPJ na cabeça."""
    fonte = APP_JS.read_text(encoding="utf-8")

    assert "const EXEMPLO = EXEMPLOS[0];" in fonte
    assert "21.278.675/0001-77" in bloco_dos_exemplos()


def test_os_estados_honestos_tem_botao_proprio() -> None:
    """A inversão desta demonstração: os estados que não são sucesso **são** o
    achado, e uma demo que só mostra o caminho encontrado esconde a tese.

    Um visitante que clica nos quatro entende o projeto sem ler o README: a
    maioria das empresas não tem sócio, a maioria dos pares não se alcança,
    quando se alcança não são seis graus, e há estrutura que não cabe na tela.
    """
    bloco = bloco_dos_exemplos()

    for demonstrado in ("74,8%", "98,41%", "22 saltos", "3.154 vizinhos"):
        assert demonstrado in bloco, f"falta o exemplo que demonstra {demonstrado}"


def test_os_rotulos_dizem_o_que_o_exemplo_demonstra() -> None:
    """ "Exemplo A" não diz nada e ninguém clica. O rótulo carrega o achado, e de
    quebra ensina o vocabulário do projeto."""
    rotulos = re.findall(r'rotulo: "([^"]+)"', bloco_dos_exemplos())

    assert len(rotulos) >= 6
    assert not any(re.fullmatch(r"Exemplo \w+", rotulo) for rotulo in rotulos)
    assert all(len(rotulo) > 25 for rotulo in rotulos), "rótulo curto demais para dizer algo"


def test_o_enquadramento_fica_junto_dos_exemplos(cliente: Any) -> None:
    """Combinado desde a curadoria: perto dos exemplos, não em rodapé.

    O visitante generaliza a partir de três casos escolhidos a dedo se ninguém
    disser, onde ele olha, que os casos não são a mensagem.
    """
    pagina = " ".join(cliente.get("/").text.split())
    exemplos = pagina.index("botoes-de-exemplo")
    enquadramento = pagina.index("Estes exemplos existem para mostrar")
    resultado_ = pagina.index('id="resultado"')

    assert exemplos < enquadramento < resultado_, "o enquadramento vem antes da resposta"
    assert "não para mostrar o que o dado significa" in pagina
    assert "decisão de modelagem" in pagina, "o critério do grau é julgamento, e é dito"


def test_a_pagina_nao_gasta_o_limite_de_consultas(grafo_de_exemplo: Config) -> None:  # noqa: F811
    """Limitar o HTML faria o limitador barrar quem só abriu o site."""
    apertado = grafo_de_exemplo.model_copy(update={"limite_por_minuto": 2})

    with TestClient(criar_aplicacao(apertado)) as aberto:
        codigos = [aberto.get("/").status_code for _ in range(5)]
        codigos += [aberto.get("/static/estilo.css").status_code for _ in range(5)]

    assert set(codigos) == {200}


def test_a_pagina_fica_fora_do_openapi(cliente: Any) -> None:
    """`/docs` descreve a API. A página não é API."""
    caminhos = cliente.get("/openapi.json").json()["paths"]

    assert "/" not in caminhos
    assert "/caminho" in caminhos


def test_a_pagina_declara_a_pseudonimizacao(cliente: Any) -> None:
    """O visitante precisa saber por que uma pessoa aparece como `Sócio 2` — sem
    isso o rótulo se lê como dado faltando."""
    texto = " ".join(cliente.get("/").text.split())

    assert "pseudonimizadas" in texto
    assert "estrutura de rede" in texto, "o enquadramento fica na página, não no README"


def test_a_partida_falha_se_a_pagina_nao_veio_no_pacote(
    grafo_de_exemplo: Config,  # noqa: F811
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Aplicação que sobe servindo `404` na raiz é pior que a que não sobe: o
    defeito só aparece pelo visitante."""
    from grafo_societario.api import web

    monkeypatch.setattr(web, "PAGINA", PAGINA.with_name("nao-existe.html"))

    with pytest.raises(web.PaginaAusenteError, match="empacotamento"):
        criar_aplicacao(grafo_de_exemplo)
