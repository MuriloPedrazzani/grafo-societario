"""Servidor WebDAV falso, em socket real.

Um mock de biblioteca provaria que o código chama o que eu mandei chamar. Um
servidor HTTP de verdade prova que ele negocia `Range`, respeita `If-Range` e
lida com resposta em pedaços — que é onde o download de arquivos de 2 GB
realmente falha.
"""

from __future__ import annotations

import os
import threading
from collections.abc import Iterator
from dataclasses import dataclass, field
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Final, cast
from xml.sax.saxutils import escape

import pytest
from hypothesis import settings

from grafo_societario.config import Config


@dataclass
class EstadoDoServidor:
    """Conteúdo servido e falhas a simular. Os testes montam isto."""

    competencias: dict[str, dict[str, bytes]] = field(default_factory=dict)
    arquivos_na_raiz: dict[str, int] = field(default_factory=dict)
    etags: dict[str, str] = field(default_factory=dict)
    falhas_restantes: int = 0
    requisicoes: list[tuple[str, str, dict[str, str]]] = field(default_factory=list)
    tamanho_anunciado: dict[str, int] = field(default_factory=dict)

    def etag_de(self, competencia: str, nome: str) -> str:
        return self.etags.get(f"{competencia}/{nome}", f'"etag-de-{nome}"')

    def cabecalhos_de(self, metodo: str, sufixo_do_caminho: str) -> dict[str, str]:
        for registrado, caminho, cabecalhos in self.requisicoes:
            if registrado == metodo and caminho.endswith(sufixo_do_caminho):
                return cabecalhos
        return {}


class ServidorFalso(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = True
    estado: EstadoDoServidor


MODIFICADO_EM = "Sun, 14 Jun 2026 19:07:57 GMT"


def _multistatus(entradas: list[tuple[str, bool, int, str]]) -> bytes:
    partes = ['<?xml version="1.0"?><d:multistatus xmlns:d="DAV:">']
    for href, colecao, tamanho, etag in entradas:
        if colecao:
            prop = "<d:resourcetype><d:collection/></d:resourcetype>"
        else:
            prop = (
                "<d:resourcetype/>"
                f"<d:getcontentlength>{tamanho}</d:getcontentlength>"
                f"<d:getetag>{escape(etag)}</d:getetag>"
                f"<d:getlastmodified>{MODIFICADO_EM}</d:getlastmodified>"
            )
        partes.append(
            f"<d:response><d:href>{escape(href)}</d:href><d:propstat><d:prop>{prop}</d:prop>"
            "<d:status>HTTP/1.1 200 OK</d:status></d:propstat></d:response>"
        )
    partes.append("</d:multistatus>")
    return "".join(partes).encode("utf-8")


class _Manipulador(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    @property
    def estado(self) -> EstadoDoServidor:
        return cast("ServidorFalso", self.server).estado

    def log_message(self, format: str, *args: object) -> None:
        """Silencia o log do servidor para não poluir a saída da suíte."""

    def _registrar(self) -> None:
        cabecalhos = {chave.lower(): valor for chave, valor in self.headers.items()}
        self.estado.requisicoes.append((self.command, self.path, cabecalhos))

    def _responder(self, status: HTTPStatus, corpo: bytes, extras: dict[str, str]) -> None:
        self.send_response(status)
        for chave, valor in extras.items():
            self.send_header(chave, valor)
        self.send_header("Content-Length", str(len(corpo)))
        self.end_headers()
        if corpo:
            self.wfile.write(corpo)

    def _partes_do_caminho(self) -> list[str]:
        return [parte for parte in self.path.split("/") if parte]

    def do_PROPFIND(self) -> None:
        self._registrar()
        partes = self._partes_do_caminho()
        # O prefixo é /public.php/dav/files/<token>; o que interessa vem depois.
        resto = partes[4:] if len(partes) >= 4 else []
        prefixo = "/" + "/".join(partes[:4])

        if not resto:
            entradas: list[tuple[str, bool, int, str]] = [(f"{prefixo}/", True, 0, "")]
            entradas += [
                (f"{prefixo}/{nome}/", True, 0, "") for nome in sorted(self.estado.competencias)
            ]
            entradas += [
                (f"{prefixo}/{nome}", False, tamanho, f'"etag-{nome}"')
                for nome, tamanho in sorted(self.estado.arquivos_na_raiz.items())
            ]
        else:
            competencia = resto[0]
            arquivos = self.estado.competencias.get(competencia)
            if arquivos is None:
                self._responder(HTTPStatus.NOT_FOUND, b"", {})
                return
            entradas = [(f"{prefixo}/{competencia}/", True, 0, "")]
            entradas += [
                (
                    f"{prefixo}/{competencia}/{nome}",
                    False,
                    self.estado.tamanho_anunciado.get(nome, len(conteudo)),
                    self.estado.etag_de(competencia, nome),
                )
                for nome, conteudo in sorted(arquivos.items())
            ]

        self._responder(
            HTTPStatus.MULTI_STATUS,
            _multistatus(entradas),
            {"Content-Type": 'application/xml; charset="utf-8"'},
        )

    def do_GET(self) -> None:
        self._registrar()

        if self.estado.falhas_restantes > 0:
            self.estado.falhas_restantes -= 1
            self._responder(HTTPStatus.SERVICE_UNAVAILABLE, b"", {})
            return

        partes = self._partes_do_caminho()
        if len(partes) < 6:
            self._responder(HTTPStatus.NOT_FOUND, b"", {})
            return
        competencia, nome = partes[4], partes[5]
        conteudo = self.estado.competencias.get(competencia, {}).get(nome)
        if conteudo is None:
            self._responder(HTTPStatus.NOT_FOUND, b"", {})
            return

        etag = self.estado.etag_de(competencia, nome)
        faixa = self.headers.get("Range")
        if_range = self.headers.get("If-Range")

        # Semântica de If-Range: a retomada só vale se o recurso não mudou.
        retomar = bool(faixa) and (if_range is None or if_range == etag)
        if retomar and faixa is not None:
            inicio = int(faixa.removeprefix("bytes=").split("-")[0])
            recorte = conteudo[inicio:]
            self._responder(
                HTTPStatus.PARTIAL_CONTENT,
                recorte,
                {
                    "Content-Type": "application/zip",
                    "ETag": etag,
                    "Content-Range": f"bytes {inicio}-{len(conteudo) - 1}/{len(conteudo)}",
                },
            )
            return

        self._responder(
            HTTPStatus.OK,
            conteudo,
            {"Content-Type": "application/zip", "ETag": etag, "Accept-Ranges": "bytes"},
        )


@pytest.fixture
def servidor() -> Iterator[tuple[str, EstadoDoServidor]]:
    servidor_http = ServidorFalso(("127.0.0.1", 0), _Manipulador)
    servidor_http.estado = EstadoDoServidor()
    thread = threading.Thread(target=servidor_http.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{servidor_http.server_port}", servidor_http.estado
    finally:
        servidor_http.shutdown()
        servidor_http.server_close()
        thread.join(timeout=5)


@pytest.fixture
def config_de_teste(servidor: tuple[str, EstadoDoServidor], tmp_path: Path) -> Config:
    """Configuração apontada para o servidor falso.

    Todos os campos vão como argumento explícito: em pydantic-settings o valor de
    inicialização tem prioridade sobre ambiente e `.env`, então a suíte não depende
    do que existe na máquina de quem roda.
    """
    url, _ = servidor
    return Config(
        competencia="2026-06",
        uf_alvo="SP",
        data_dir=tmp_path,
        rfb_url_base=url,
        rfb_token_compartilhamento="token-de-teste",
    )


# ------------------------------------------------- Hypothesis, e o que ele pode sortear

PERFIL_PADRAO: Final = "reprodutivel"
"""Perfil usado quando `HYPOTHESIS_PROFILE` não diz outra coisa.

**Teste que falha sem poder ser reproduzido é pior que teste ausente.** O padrão
do Hypothesis é sortear a cada execução e guardar um banco de exemplos entre elas,
o que produz vermelho no CI que não acontece na máquina de quem vai investigar.

As duas decisões que evitam isso, e o motivo de cada uma:

- **`derandomize=True`.** A geração passa a ser função determinística do teste, e
  não do relógio. Um vermelho no CI reproduz rodando o mesmo comando localmente,
  sem precisar do banco nem da semente que o runner sorteou.
- **`database=None`.** Sem banco, não há estado carregado entre execuções, e
  também não há por que versioná-lo: com a geração já determinística, o banco não
  acrescenta reprodutibilidade — só acrescenta um arquivo que muda sozinho.

O custo está declarado: derandomize troca exploração ao longo do tempo por
repetição fiel. Quem quiser procurar caso novo roda com
`HYPOTHESIS_PROFILE=exploratorio`, que sorteia e mantém banco local.
"""

settings.register_profile(
    PERFIL_PADRAO,
    derandomize=True,
    database=None,
    # Sem prazo por exemplo, e isto é decisão e não descuido. Prazo de relógio é
    # asserção dependente de plataforma: o mesmo caso passa na máquina rápida e
    # reprova no runner compartilhado, exatamente como o limiar de readahead que
    # reprovou no CI da Fase 4. O que limita o trabalho aqui é `max_examples` e o
    # tamanho dos grafos gerados, que são determinísticos.
    deadline=None,
    print_blob=True,
)
settings.register_profile(
    "exploratorio",
    derandomize=False,
    deadline=None,
    max_examples=1000,
)
settings.load_profile(os.environ.get("HYPOTHESIS_PROFILE", PERFIL_PADRAO))
