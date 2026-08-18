"""O limitador e o tratamento de erro, que são as duas bordas do serviço.

O limitador existe contra **varredura**, e não contra pico de visitante. Se o
link circular e cinquenta pessoas clicarem ao mesmo tempo, travá-las mata
exatamente aquilo que a fase existe para mostrar — por isso o balde é por
cliente, e generoso para quem está olhando.
"""

from __future__ import annotations

import time
from concurrent.futures import ThreadPoolExecutor
from typing import Any

import pytest
from fastapi.testclient import TestClient

from grafo_societario.api.cnpj import formatar
from grafo_societario.api.limite import JANELA, Limitador, cliente_de
from grafo_societario.api.main import criar_aplicacao
from grafo_societario.config import Config
from grafo_societario.graph.csr import NoForaDaFaixaError
from test_caminho import ALFA, BRAVO, grafo_de_exemplo  # noqa: F401


@pytest.fixture
def cliente(grafo_de_exemplo: Config) -> Any:  # noqa: F811
    apertado = grafo_de_exemplo.model_copy(update={"limite_por_minuto": 3})
    with TestClient(criar_aplicacao(apertado)) as aberto:
        yield aberto


def consultar(cliente: Any, **cabecalhos: str) -> Any:
    return cliente.get(
        "/caminho",
        params={"de": formatar(int(ALFA)), "para": formatar(int(BRAVO))},
        headers=cabecalhos or None,
    )


# ----------------------------------------------------------- o balde e o 429


def test_dentro_do_limite_responde_e_acima_dele_recusa(cliente: Any) -> None:
    permitidas = [consultar(cliente).status_code for _ in range(3)]
    barrada = consultar(cliente)

    assert permitidas == [200, 200, 200]
    assert barrada.status_code == 429


def test_o_429_leva_retry_after(cliente: Any) -> None:
    """É a diferença entre limite usável e muro: sem ele o cliente não sabe
    quando voltar, e a escolha dele vira tentar de novo já."""
    for _ in range(4):
        resposta = consultar(cliente)

    assert resposta.status_code == 429
    assert 1 <= int(resposta.headers["Retry-After"]) <= JANELA


def test_o_429_diz_onde_pegar_o_artefato_inteiro(cliente: Any) -> None:
    """Quem quer os 19,7 milhões deve baixar o Release, não varrer a rota.

    Um limite que só diz "não" empurra para a varredura mais lenta. Dizer onde
    está o caminho sancionado é o que torna a varredura desnecessária.
    """
    for _ in range(4):
        resposta = consultar(cliente)

    assert "Release" in resposta.json()["detail"]


def test_clientes_diferentes_tem_baldes_diferentes(grafo_de_exemplo: Config) -> None:  # noqa: F811
    """Cinquenta pessoas clicando no mesmo link não podem se atrapalhar."""
    apertado = grafo_de_exemplo.model_copy(update={"limite_por_minuto": 2, "proxies_confiaveis": 1})
    with TestClient(criar_aplicacao(apertado)) as aberto:
        gastos = [
            consultar(aberto, **{"x-forwarded-for": "10.0.0.1"}).status_code for _ in range(3)
        ]
        outro = consultar(aberto, **{"x-forwarded-for": "10.0.0.2"})

    assert gastos == [200, 200, 429]
    assert outro.status_code == 200


def test_o_health_nao_e_limitado(cliente: Any) -> None:
    """A plataforma consulta o health para decidir se manda tráfego. Limitá-lo
    faria o limitador derrubar a própria instância."""
    for _ in range(10):
        assert cliente.get("/health").status_code == 200


# ------------------------------------------- a identidade do cliente é forjável


def test_sem_proxy_confiavel_o_cabecalho_encaminhado_e_ignorado(
    grafo_de_exemplo: Config,  # noqa: F811
) -> None:
    """`X-Forwarded-For` é escrito por quem chama, e trocá-lo a cada requisição
    fura qualquer limite baseado nele.

    Com zero saltos confiáveis vale o IP da conexão, que é o único que não se
    forja — e aí forjar o cabeçalho não compra balde novo.
    """
    apertado = grafo_de_exemplo.model_copy(update={"limite_por_minuto": 2, "proxies_confiaveis": 0})
    with TestClient(criar_aplicacao(apertado)) as aberto:
        codigos = [
            consultar(aberto, **{"x-forwarded-for": f"10.0.0.{i}"}).status_code for i in range(3)
        ]

    assert codigos == [200, 200, 429], "trocar o cabeçalho não pode zerar o balde"


@pytest.mark.parametrize(
    ("saltos", "encaminhado", "esperado"),
    [
        (0, "1.1.1.1, 2.2.2.2", "conexao"),
        (1, "1.1.1.1, 2.2.2.2", "2.2.2.2"),
        (2, "1.1.1.1, 2.2.2.2", "1.1.1.1"),
        (1, "", "conexao"),
        (2, "2.2.2.2", "conexao"),
    ],
)
def test_confia_apenas_nos_saltos_declarados(saltos: int, encaminhado: str, esperado: str) -> None:
    """O cabeçalho é acrescentado da esquerda para a direita, então a entrada
    mais à direita é a que o proxy **mais próximo** escreveu.

    Confiar em `saltos` significa contar da direita para a esquerda essa
    quantidade. Tudo o que estiver à esquerda disso veio do cliente e é forjável.
    """

    class Falsa:
        headers = {"x-forwarded-for": encaminhado} if encaminhado else {}
        client = type("C", (), {"host": "conexao"})()

    assert cliente_de(Falsa(), saltos) == esperado  # type: ignore[arg-type]


# ------------------------------------------------- concorrência sobre o contador


def test_o_contador_nao_perde_requisicao_sob_concorrencia() -> None:
    """O limitador é estado mutável compartilhado, e aqui isso é inevitável.

    O threadpool do uvicorn chama isto de várias threads ao mesmo tempo, e um
    contador sem sincronização perde incrementos — o que faz o limite deixar
    passar mais do que promete, em silêncio.
    """
    limitador = Limitador(por_minuto=10_000, proxies_confiaveis=0)
    agora = time.monotonic()

    with ThreadPoolExecutor(max_workers=16) as piscina:
        list(piscina.map(lambda _: limitador.registrar("mesmo", agora), range(4_000)))

    assert limitador.contagem_de("mesmo") == 4_000


def test_a_janela_vira_e_zera_o_contador() -> None:
    limitador = Limitador(por_minuto=2, proxies_confiaveis=0)

    assert limitador.registrar("x", 0.0)[0] is True
    assert limitador.registrar("x", 0.0)[0] is True
    assert limitador.registrar("x", 0.0)[0] is False
    assert limitador.registrar("x", JANELA + 0.1)[0] is True


# ------------------------------------------------- nenhum 500 genérico


@pytest.fixture
def tolerante(grafo_de_exemplo: Config) -> Any:  # noqa: F811
    """Cliente que **não** relança a exceção do servidor, para ver a resposta."""
    with TestClient(criar_aplicacao(grafo_de_exemplo), raise_server_exceptions=False) as aberto:
        yield aberto


def quebrar(tolerante: Any, monkeypatch: pytest.MonkeyPatch, excecao: Exception) -> Any:
    def explodir(*args: Any, **kwargs: Any) -> Any:
        raise excecao

    monkeypatch.setattr("grafo_societario.api.caminho.buscar_caminho", explodir)
    return consultar(tolerante)


def test_excecao_nao_tratada_vira_resposta_com_identificador(
    tolerante: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    resposta = quebrar(tolerante, monkeypatch, RuntimeError("qualquer coisa"))

    assert resposta.status_code == 500
    corpo = resposta.json()
    assert len(corpo["erro_id"]) == 12
    assert corpo["detail"]


def test_a_excecao_interna_nao_chega_ao_cliente(
    tolerante: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """`NoForaDaFaixaError` e as do CSR descrevem artefato inconsistente. São
    defeito, não resposta — e vazá-las entrega o mapa das entranhas junto."""
    interna = NoForaDaFaixaError("O nó -1 está fora da faixa 0..2")

    resposta = quebrar(tolerante, monkeypatch, interna)

    inteiro = resposta.text
    assert resposta.status_code == 500
    assert "NoForaDaFaixaError" not in inteiro
    assert "fora da faixa" not in inteiro
    assert "Traceback" not in inteiro


def test_cada_erro_tem_identificador_proprio(
    tolerante: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Dois relatos diferentes precisam apontar para duas linhas diferentes."""
    primeiro = quebrar(tolerante, monkeypatch, RuntimeError("a")).json()["erro_id"]
    segundo = quebrar(tolerante, monkeypatch, RuntimeError("b")).json()["erro_id"]

    assert primeiro != segundo


def test_o_identificador_vai_para_o_log(
    tolerante: Any, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """Sem isto o identificador é enfeite: o cliente cita um código que não
    existe em lugar nenhum."""
    with caplog.at_level("ERROR"):
        erro_id = quebrar(tolerante, monkeypatch, RuntimeError("x")).json()["erro_id"]

    registros = [r for r in caplog.records if getattr(r, "erro_id", None) == erro_id]
    assert registros, "o erro devolvido ao cliente não apareceu no log"
    assert registros[0].exc_info is not None, "o rastro fica no log, que é onde ele serve"


@pytest.mark.parametrize(
    ("caminho_pedido", "parametros", "esperado"),
    [
        ("/caminho", {"de": "11111111", "para": "22222222000191"}, 422),
        ("/empresa/66666666000191", {}, 404),
    ],
)
def test_as_respostas_legitimas_nao_viram_500(
    tolerante: Any, caminho_pedido: str, parametros: dict[str, str], esperado: int
) -> None:
    """O tratador cobre o que ninguém previu, e não o que as rotas decidiram."""
    assert tolerante.get(caminho_pedido, params=parametros).status_code == esperado


def test_o_health_declara_a_configuracao_do_limitador(cliente: Any) -> None:
    """Quem opera precisa ver quantos saltos estão sendo confiados: com zero
    atrás de um proxy, todos os visitantes caem no mesmo balde."""
    limite = cliente.get("/health").json()["limite"]

    assert limite["por_minuto"] == 3
    assert limite["proxies_confiaveis"] == 0


def test_a_janela_que_vira_esvazia_o_dicionario() -> None:
    """Sem isso, um cliente que troca de IP a cada requisição faz o dicionário
    crescer sem teto, e o limitador vira o vazamento."""
    limitador = Limitador(por_minuto=10, proxies_confiaveis=0)
    for i in range(500):
        limitador.registrar(f"ip-{i}", 0.0)

    limitador.registrar("outro", JANELA + 0.1)

    assert limitador.rastreados() == 1
