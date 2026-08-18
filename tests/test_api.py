"""A partida da aplicação: ela morre com o artefato quebrado, ou não sobe mentindo.

O teste central deste arquivo não consulta nada — ele **quebra um artefato e exige
que a aplicação não suba**. Health check verde com grafo não carregado é pior que
processo morto: um você percebe, o outro você descobre pelo usuário, depois de o
balanceador já ter mandado tráfego.
"""

from __future__ import annotations

import builtins
import os
import subprocess
import sys
import zlib
from collections.abc import Iterator
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from dataclasses import fields
from pathlib import Path
from typing import Any
from unittest import mock

import numpy as np
import pytest
from fastapi.testclient import TestClient

from grafo_societario.api.deps import (
    Acervo,
    AcervoDep,
    AcervoIndisponivelError,
    ErroDePartida,
    carregar_acervo,
)
from grafo_societario.api.main import criar_aplicacao
from grafo_societario.config import Config
from grafo_societario.graph.artefatos import ARTEFATOS_PUBLICAVEIS, somas_dos_artefatos
from grafo_societario.graph.build import gerar_arestas, gerar_nos, serializar_csr
from grafo_societario.graph.catalogo import (
    ArtefatoAusenteError,
    ArtefatosIncompativeisError,
    Catalogo,
    abrir_catalogo,
)
from grafo_societario.graph.components import calcular_componentes
from grafo_societario.graph.csr import ArtefatoAusenteError as CsrAusenteError
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


@pytest.fixture
def pronto(tmp_path: Path) -> Config:
    """Um grafo completo em disco: duas empresas ligadas por uma pessoa física."""
    config = Config(competencia="2026-06", data_dir=tmp_path, uf_alvo="SP")
    gravar_estabelecimentos(
        config, [estabelecimento(cnpj) for cnpj in ("11111111", "22222222", "33333333")]
    )
    aplicar_recorte_por_uf(config)
    gravar_empresas(
        config,
        [
            empresa("11111111", razao_social="ALFA COMERCIO LTDA"),
            empresa("22222222", razao_social="BRAVO SERVICOS SA"),
            empresa("33333333", razao_social="CHARLIE SOZINHA ME"),
        ],
    )
    _gravar_dominio(config, "Naturezas", NATUREZAS_PADRAO)
    _gravar_dominio(config, "Qualificacoes", QUALIFICACOES_PADRAO)
    _gravar_dominio(config, "Paises", PAISES_PADRAO)
    tipar_empresas(config)
    gravar_socios(
        config,
        [
            socio("11111111", nome="FULANO DE TAL", documento="***123458**"),
            socio("22222222", nome="FULANO DE TAL", documento="***123458**"),
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


def pasta(config: Config) -> Path:
    return config.data_dir / "grafo" / "2026-06"


# ------------------------------------------------------------------ os instrumentos


@contextmanager
def contar_aberturas(pasta_do_grafo: Path) -> Iterator[list[str]]:
    """Nomes dos arquivos da pasta do grafo **abertos** enquanto o bloco roda.

    Conta abertura, e não leitura. `mmap` lê por falta de página, sem passar por
    aqui, e é assim que ele foi feito: quem promete "a segunda requisição não
    relê disco" promete o que o `mmap` não faz. O que dá para prometer é que o
    artefato não é **reaberto** — ver o topo de `api/deps.py`.
    """
    original = builtins.open
    vistas: list[str] = []

    def espiao(arquivo: Any, *args: Any, **kwargs: Any) -> Any:
        try:
            caminho: Path | None = Path(os.fspath(arquivo))
        except TypeError:  # descritor numérico, que não é caminho de nada
            caminho = None
        if caminho is not None and caminho.parent == pasta_do_grafo:
            vistas.append(caminho.name)
        return original(arquivo, *args, **kwargs)

    with mock.patch("builtins.open", espiao):
        yield vistas


@contextmanager
def contar_descompressoes() -> Iterator[list[int]]:
    """Tamanho de cada bloco `zlib` aberto enquanto o bloco roda."""
    original = zlib.decompress
    tamanhos: list[int] = []

    def espiao(dado: Any, *args: Any, **kwargs: Any) -> Any:
        aberto = original(dado, *args, **kwargs)
        tamanhos.append(len(aberto))
        return aberto

    with mock.patch("zlib.decompress", espiao):
        yield tamanhos


def _arrays_do(acervo: Acervo) -> list[tuple[str, np.ndarray[Any, np.dtype[Any]]]]:
    """Todo array alcançável a partir do acervo, com o nome de onde ele veio.

    Varre os campos em vez de listá-los para que um array acrescentado adiante
    entre na conferência sem ninguém lembrar de incluí-lo.
    """
    donos: tuple[Any, ...] = (acervo, acervo.grafo, acervo.catalogo)
    return [
        (f"{type(dono).__name__}.{campo.name}", valor)
        for dono in donos
        for campo in fields(dono)
        if isinstance(valor := getattr(dono, campo.name), np.ndarray)
    ]


def _ler_tudo(acervo: Acervo) -> list[tuple[Any, ...]]:
    """Uma leitura de cada coisa que uma resposta faz, sobre todos os nós."""
    return [
        (
            acervo.catalogo.nome_de(no),
            acervo.catalogo.tipo_de(no),
            acervo.catalogo.cnpj_basico_de(no),
            acervo.catalogo.confianca_de(no),
            acervo.catalogo.regiao_de(no),
            tuple(int(vizinho) for vizinho in acervo.grafo.vizinhos(no)),
            int(acervo.componentes[no]),
        )
        for no in range(acervo.nos)
    ]


def _no_com_nome(catalogo: Catalogo) -> int:
    for indice in range(catalogo.nos):
        if catalogo.nome_de(indice) is not None:
            return indice
    raise AssertionError("a fixture precisa de ao menos um nó com nome")


def _no_sem_nome(catalogo: Catalogo) -> int:
    for indice in range(catalogo.nos):
        if catalogo.nome_de(indice) is None:
            return indice
    raise AssertionError("a fixture precisa de ao menos um nó sem nome")


# ------------------------------------------------ a aplicação sobe e diz o que carregou


def test_o_health_diz_qual_dado_esta_no_ar(pronto: Config) -> None:
    with TestClient(criar_aplicacao(pronto)) as cliente:
        resposta = cliente.get("/health")

    assert resposta.status_code == 200
    corpo = resposta.json()
    assert corpo["status"] == "ok"
    assert corpo["competencia"] == "2026-06"
    assert corpo["uf_alvo"] == "SP"
    assert corpo["expor_pf"] is False
    assert corpo["grafo"]["nos"] == 3
    assert corpo["grafo"]["empresas_no_recorte"] == 3


def test_o_health_traz_a_soma_de_cada_artefato(pronto: Config) -> None:
    """A competência diz de qual mês é o dado; a soma diz qual execução o produziu.

    É o que responde "qual build está no ar" sem adivinhação — e só significa algo
    porque o projeto garante que duas construções sobre o mesmo silver dão os
    mesmos bytes.
    """
    with TestClient(criar_aplicacao(pronto)) as cliente:
        somas = cliente.get("/health").json()["artefatos"]

    assert set(somas) == set(ARTEFATOS_PUBLICAVEIS)
    assert somas == somas_dos_artefatos(pasta(pronto))
    assert all(len(soma) == 64 for soma in somas.values())


def test_a_soma_muda_quando_o_artefato_muda(pronto: Config) -> None:
    """Controle positivo da soma: um medidor que devolve sempre o mesmo valor não
    identifica build nenhum."""
    antes = somas_dos_artefatos(pasta(pronto))
    alvo = pasta(pronto) / "regiao_fiscal.npy"
    with alvo.open("wb") as arquivo:
        np.save(arquivo, np.zeros(3, dtype=np.int8), allow_pickle=False)

    depois = somas_dos_artefatos(pasta(pronto))

    assert depois["regiao_fiscal.npy"] != antes["regiao_fiscal.npy"]
    assert depois["indptr.npy"] == antes["indptr.npy"], "o resto não pode mudar"


def test_o_health_declara_a_versao_do_pacote(pronto: Config) -> None:
    from grafo_societario import __version__

    with TestClient(criar_aplicacao(pronto)) as cliente:
        assert cliente.get("/health").json()["versao"] == __version__


def test_a_documentacao_interativa_responde(pronto: Config) -> None:
    """`/docs` é o link que vai para o currículo; se ele não abre, a fase não
    entregou o que prometeu."""
    with TestClient(criar_aplicacao(pronto)) as cliente:
        assert cliente.get("/docs").status_code == 200
        assert cliente.get("/openapi.json").status_code == 200


# ------------------------------------------- a aplicação morre na partida


@pytest.mark.parametrize(
    "ausente", ["indptr.npy", "nomes.bin", "componentes.npy", "existencia.npy"]
)
def test_artefato_ausente_derruba_a_partida(pronto: Config, ausente: str) -> None:
    """Não é erro na primeira consulta: é a aplicação não existir.

    Se o `/health` responde, o grafo está mapeado e conferido — e é isso que torna
    a resposta dele digna de confiança.
    """
    (pasta(pronto) / ausente).unlink()

    with (
        pytest.raises((ArtefatoAusenteError, CsrAusenteError, ErroDePartida)),
        TestClient(criar_aplicacao(pronto)),
    ):
        pass


def test_catalogo_de_outra_execucao_derruba_a_partida(pronto: Config) -> None:
    """A conferência que só existe aqui: cada artefato valida a si mesmo, e
    ninguém sozinho vê o catálogo descrevendo outro número de nós."""
    alvo = pasta(pronto) / "atributos.npy"
    with alvo.open("wb") as arquivo:
        np.save(arquivo, np.zeros(99, dtype=np.int8), allow_pickle=False)

    with pytest.raises((ErroDePartida, ArtefatosIncompativeisError)):
        carregar_acervo(pronto)


def test_arrays_do_csr_desalinhados_derrubam_a_partida(pronto: Config) -> None:
    alvo = pasta(pronto) / "indices.npy"
    with alvo.open("wb") as arquivo:
        np.save(arquivo, np.zeros(3, dtype=np.int32), allow_pickle=False)

    with pytest.raises(Exception, match=r"execuções diferentes|posições"):
        carregar_acervo(pronto)


def test_competencia_sem_artefato_derruba_a_partida(pronto: Config) -> None:
    outra = pronto.model_copy(update={"competencia": "2026-07"})

    with pytest.raises((ArtefatoAusenteError, CsrAusenteError, ErroDePartida)):
        carregar_acervo(outra)


def test_o_acervo_carrega_uma_vez_e_registra_o_custo(pronto: Config) -> None:
    acervo = carregar_acervo(pronto)

    assert acervo.segundos_de_partida > 0
    assert acervo.nos == 3
    assert acervo.arestas == 2
    assert acervo.catalogo.nos == acervo.grafo.nos


# ------------------------------------------- carrega uma vez: não reabre o artefato


def test_o_contador_de_aberturas_enxerga_quem_reabre(pronto: Config) -> None:
    """Controle negativo do instrumento, sem o qual "zero aberturas" não vale nada.

    Uma rota que recarrega o acervo a cada requisição é exatamente o defeito que
    o teste seguinte precisa excluir. Se o contador não enxerga nem ela, ele não
    enxerga coisa nenhuma, e o zero dele seria um zero de instrumento cego.
    """
    app = criar_aplicacao(pronto)

    @app.get("/_recarrega")
    def recarrega() -> dict[str, int]:
        return {"nos": carregar_acervo(pronto).nos}

    with TestClient(app) as cliente, contar_aberturas(pasta(pronto)) as vistas:
        assert cliente.get("/_recarrega").status_code == 200

    assert set(ARTEFATOS_PUBLICAVEIS) <= set(vistas)


def test_a_segunda_requisicao_nao_reabre_o_artefato(pronto: Config) -> None:
    """O que o `mmap` comprou foi partida, e é isso que este teste protege.

    Não é "não relê disco": `mmap` relê por falta de página, e a Fase 4 mediu o
    tamanho disso — abrir custa 0,07 MiB residentes e cem mil acessos aleatórios
    trazem +110 MiB. O que não pode acontecer é reabrir o artefato, remapear os
    arrays ou recalcular o SHA-256 de 416 MB a cada health check.
    """
    # O contador entra primeiro de propósito: a partida é o controle positivo dele.
    with (
        contar_aberturas(pasta(pronto)) as vistas,
        TestClient(criar_aplicacao(pronto)) as cliente,
    ):
        na_partida = list(vistas)
        vistas.clear()
        for _ in range(3):
            assert cliente.get("/health").status_code == 200
        depois_das_requisicoes = list(vistas)

    assert set(ARTEFATOS_PUBLICAVEIS) <= set(na_partida), "a partida abre tudo, e é ela que paga"
    assert depois_das_requisicoes == []


def test_toda_requisicao_recebe_o_mesmo_acervo(pronto: Config) -> None:
    """A injeção entrega o objeto da partida, e não uma cópia por requisição.

    Cópia por requisição não falha teste nenhum de conteúdo — ela responde igual.
    Falha o relógio e a memória, que é a forma de defeito que este projeto trata
    como a pior.
    """
    app = criar_aplicacao(pronto)
    vistos: list[Acervo] = []

    @app.get("/_espiar")
    def espiar(acervo: AcervoDep) -> dict[str, str]:
        vistos.append(acervo)
        return {"competencia": acervo.competencia}

    with TestClient(app) as cliente:
        for _ in range(3):
            assert cliente.get("/_espiar").status_code == 200

    assert len(vistos) == 3
    assert all(visto is vistos[0] for visto in vistos)


def test_rota_sem_lifespan_diz_que_o_acervo_nao_foi_carregado(pronto: Config) -> None:
    """Falta de acervo numa rota é erro de montagem, e a mensagem tem de dizer isso.

    `None` devolvido aqui viraria `AttributeError` fundo adentro, longe da causa.
    """
    cliente = TestClient(criar_aplicacao(pronto))  # sem `with`, o lifespan não roda

    with pytest.raises(AcervoIndisponivelError, match="lifespan"):
        cliente.get("/health")


# --------------------------------------- concorrência: o acervo é lido por muitas threads


def test_o_catalogo_descomprime_de_novo_a_cada_leitura_do_mesmo_no(pronto: Config) -> None:
    """Não há cache de bloco descomprimido, e a ausência é decisão medida.

    Contra o artefato real de 2026-06: um nome custa 343 µs, o mesmo nó relido
    custa os mesmos 343 µs, um caminho de 21 nós custa ~3,8 ms — e com 8 threads
    o custo por nome cai para 77 µs, porque o `zlib` solta a GIL enquanto
    descomprime.

    Se este teste falhar, alguém acrescentou cache. Ele é estado mutável
    compartilhado entre requisições simultâneas, e o lock que o protegeria
    serializaria justamente o único trecho que hoje escala.
    """
    catalogo = abrir_catalogo(pronto)
    alvo = _no_com_nome(catalogo)

    with contar_descompressoes() as tamanhos:
        primeira = catalogo.nome_de(alvo)
        segunda = catalogo.nome_de(alvo)

    assert primeira == segunda
    assert len(tamanhos) == 2


def test_no_sem_nome_nao_chega_a_abrir_bloco(pronto: Config) -> None:
    """Controle do instrumento, e propriedade real do artefato publicável.

    Pessoa física tem faixa vazia de nome, e ler o nome dela não descomprime
    nada — é o que faz o contador acima medir descompressão, e não chamada.
    """
    catalogo = abrir_catalogo(pronto)
    alvo = _no_sem_nome(catalogo)

    with contar_descompressoes() as tamanhos:
        assert catalogo.nome_de(alvo) is None

    assert tamanhos == []


def test_a_leitura_concorrente_do_acervo_devolve_o_mesmo_que_a_serial(pronto: Config) -> None:
    """O uvicorn manda endpoint síncrono para threadpool: o mesmo `Acervo` atende
    requisições simultâneas.

    Isso só é seguro porque não há estado mutável — nem cache de bloco, nem array
    gravável. No dia em que houver, é aqui que a falta de sincronização aparece.
    """
    acervo = carregar_acervo(pronto)
    serial = _ler_tudo(acervo)

    with ThreadPoolExecutor(max_workers=8) as piscina:
        paralelo = list(piscina.map(lambda _: _ler_tudo(acervo), range(32)))

    assert all(leitura == serial for leitura in paralelo)


def test_nenhum_array_do_acervo_e_gravavel(pronto: Config) -> None:
    """Compartilhado entre threads e gravável é a combinação que não pode existir.

    Os mapeamentos abrem em modo `"r"` e já recusam escrita. Os dois arrays que o
    catálogo deriva na abertura nascem em memória comum e sairiam graváveis:
    nada escreve neles hoje, mas isso é convenção, e convenção não sobrevive ao
    próximo commit que passar por perto.
    """
    acervo = carregar_acervo(pronto)
    arrays = _arrays_do(acervo)

    graveis = [nome for nome, array in arrays if array.flags.writeable]

    assert len(arrays) >= len(ARTEFATOS_PUBLICAVEIS), (
        "o varredor não achou os arrays do acervo; sem isso, ver zero gravável não prova nada"
    )
    assert not graveis, f"gravável e compartilhado entre requisições: {', '.join(graveis)}"


# ------------------------- a fronteira entre serving e construção


def test_a_aplicacao_nao_carrega_motor_nem_leitor_de_parquet() -> None:
    """A regra que atravessa a fase inteira, afirmada no módulo que responde.

    A imagem da Fase 8 tem teto de 300 MB. Se a aplicação arrastasse DuckDB,
    SciPy ou pyarrow, o catálogo do commit anterior não teria razão de existir —
    o Parquet já estava lá.
    """
    codigo = (
        "import sys; import grafo_societario.api.main; "
        "print('duckdb' in sys.modules, 'scipy' in sys.modules, 'pyarrow' in sys.modules)"
    )

    saida = subprocess.run(
        [sys.executable, "-c", codigo], capture_output=True, text=True, check=True
    )

    assert saida.stdout.strip() == "False False False", saida.stdout
