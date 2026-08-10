"""Download dos arquivos da Receita Federal a partir do compartilhamento público.

A distribuição deixou de ser listagem de diretório no fim de janeiro de 2026 e
passou a um compartilhamento Nextcloud. A página é renderizada por JavaScript,
mas o WebDAV público responde direto: `PROPFIND` lista, `GET` baixa, e nada disso
exige credencial além do próprio token do compartilhamento.

Três decisões estruturam este módulo:

**Lista de permissão, não de exclusão.** Só é baixado o que está em
`ARQUIVOS_ESPERADOS`. Uma lista de exclusão protegeria contra o `cnpj.tar.gz` de
60 GiB que hoje está na raiz do compartilhamento, mas não contra o próximo
arquivo que aparecer lá — e é justamente esse que ninguém vai ver a tempo.

**Competência é validada antes de ser aceita.** Existir uma pasta não significa
que o envio dela terminou. Faltando qualquer arquivo esperado, a falha é alta e
imediata, em vez de virar um recorte silenciosamente incompleto lá na frente.

**Retomada é condicionada ao `ETag`.** O parcial em disco só continua se o
arquivo remoto não mudou, o que é exatamente a semântica de `If-Range`: o
servidor devolve `206` para continuar ou `200` para recomeçar, e quem decide é
ele. O `ETag` do Nextcloud **não** é o hash do conteúdo — verificação de
integridade é assunto do manifesto, não daqui.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from pathlib import Path
from typing import Final
from urllib.parse import unquote
from xml.etree import ElementTree

import httpx
from tenacity import (
    RetryCallState,
    Retrying,
    retry_if_exception_type,
    stop_after_attempt,
    wait_exponential,
)

from grafo_societario.config import Config

logger = logging.getLogger(__name__)

_DAV: Final = "{DAV:}"
_BLOCO: Final = 1024 * 1024
_TENTATIVAS: Final = 5
_TRANSITORIOS: Final = frozenset({408, 429, 500, 502, 503, 504})

# Não são Final de propósito: a suíte reduz a espera a zero para exercitar a
# retentativa sem gastar dezenas de segundos de relógio.
ESPERA_MINIMA = 1.0
ESPERA_MAXIMA = 30.0


def _nomes_esperados() -> frozenset[str]:
    particionados = [
        f"{grupo}{indice}.zip"
        for grupo in ("Empresas", "Estabelecimentos", "Socios")
        for indice in range(10)
    ]
    tabelas = [
        "Cnaes.zip",
        "Motivos.zip",
        "Municipios.zip",
        "Naturezas.zip",
        "Paises.zip",
        "Qualificacoes.zip",
    ]
    return frozenset(particionados + tabelas)


ARQUIVOS_ESPERADOS: Final = _nomes_esperados()
"""Os 36 arquivos que o MVP consome. `Simples.zip` existe na origem e fica de fora."""


class ErroDeIngestao(RuntimeError):
    """Falha na aquisição dos arquivos da Receita Federal."""


class CompetenciaIncompletaError(ErroDeIngestao):
    """A competência existe na origem, mas não tem todos os arquivos esperados."""


class FalhaTransitoriaError(ErroDeIngestao):
    """Falha que justifica nova tentativa: indisponibilidade ou resposta truncada."""


@dataclass(frozen=True)
class ArquivoRemoto:
    """Um arquivo do compartilhamento, como anunciado pelo servidor."""

    nome: str
    caminho: str
    tamanho: int
    etag: str


def criar_cliente(config: Config) -> httpx.Client:
    """Cliente apontado para a raiz do compartilhamento.

    O token vai como usuário do Basic auth, com senha vazia — é assim que o
    Nextcloud autentica compartilhamento público no WebDAV.
    """
    token = config.rfb_token_compartilhamento
    return httpx.Client(
        base_url=f"{config.rfb_url_base}/public.php/dav/files/{token}",
        auth=(token, ""),
        timeout=httpx.Timeout(30.0, read=120.0),
        follow_redirects=True,
    )


def _propfind(cliente: httpx.Client, caminho: str) -> ElementTree.Element:
    resposta = cliente.request("PROPFIND", caminho, headers={"Depth": "1"})
    if resposta.status_code in _TRANSITORIOS:
        raise FalhaTransitoriaError(f"listagem de {caminho} devolveu {resposta.status_code}")
    resposta.raise_for_status()
    return ElementTree.fromstring(resposta.text)


def _nome_do_href(href: str) -> str:
    return unquote(href).rstrip("/").rsplit("/", maxsplit=1)[-1]


def listar_competencias(cliente: httpx.Client) -> list[str]:
    """Nomes das pastas de competência, em ordem crescente."""
    raiz = _propfind(cliente, "/")
    competencias = []
    for resposta in raiz.findall(f"{_DAV}response"):
        prop = resposta.find(f"{_DAV}propstat/{_DAV}prop")
        if prop is None or prop.find(f"{_DAV}resourcetype/{_DAV}collection") is None:
            continue
        nome = _nome_do_href(resposta.findtext(f"{_DAV}href") or "")
        if len(nome) == 7 and nome[4] == "-" and nome[:4].isdigit() and nome[5:].isdigit():
            competencias.append(nome)
    return sorted(competencias)


def listar_arquivos(cliente: httpx.Client, competencia: str) -> dict[str, ArquivoRemoto]:
    """Arquivos da competência que estão na lista de permissão.

    O que não está na lista nem é olhado. É esta função que impede o
    `cnpj.tar.gz` de 60 GiB — e qualquer sucessor dele — de entrar no download.
    """
    raiz = _propfind(cliente, f"/{competencia}/")
    encontrados: dict[str, ArquivoRemoto] = {}
    ignorados: list[str] = []

    for resposta in raiz.findall(f"{_DAV}response"):
        prop = resposta.find(f"{_DAV}propstat/{_DAV}prop")
        if prop is None or prop.find(f"{_DAV}resourcetype/{_DAV}collection") is not None:
            continue
        nome = _nome_do_href(resposta.findtext(f"{_DAV}href") or "")
        if nome not in ARQUIVOS_ESPERADOS:
            ignorados.append(nome)
            continue
        tamanho = prop.findtext(f"{_DAV}getcontentlength") or "0"
        encontrados[nome] = ArquivoRemoto(
            nome=nome,
            caminho=f"/{competencia}/{nome}",
            tamanho=int(tamanho),
            etag=prop.findtext(f"{_DAV}getetag") or "",
        )

    if ignorados:
        logger.info(
            "arquivos fora da lista de permissão foram ignorados",
            extra={"competencia": competencia, "ignorados": sorted(ignorados)},
        )
    return encontrados


def validar_competencia(competencia: str, arquivos: dict[str, ArquivoRemoto]) -> None:
    """Recusa competência incompleta.

    A pasta da competência mais recente pode estar em pleno envio. Aceitar o que
    está lá produziria um recorte que parece íntegro e não é.
    """
    faltando = sorted(ARQUIVOS_ESPERADOS - arquivos.keys())
    if not faltando:
        return

    amostra = ", ".join(faltando[:5])
    reticencias = f" e mais {len(faltando) - 5}" if len(faltando) > 5 else ""
    raise CompetenciaIncompletaError(
        f"A competência {competencia} tem {len(arquivos)} dos {len(ARQUIVOS_ESPERADOS)} "
        f"arquivos esperados. Faltam: {amostra}{reticencias}. "
        "Uma competência recém-publicada pode estar em envio; tente a anterior."
    )


def _registrar_nova_tentativa(estado: RetryCallState) -> None:
    espera = getattr(estado.next_action, "sleep", 0.0)
    logger.warning(
        "download falhou, nova tentativa agendada",
        extra={
            "tentativa": estado.attempt_number,
            "espera_segundos": round(espera, 1),
            "causa": str(estado.outcome.exception()) if estado.outcome else "",
        },
    )


def _politica_de_retentativa() -> Retrying:
    """Construída a cada chamada para que a espera seja parâmetro, não constante fixa."""
    return Retrying(
        stop=stop_after_attempt(_TENTATIVAS),
        wait=wait_exponential(multiplier=1, min=ESPERA_MINIMA, max=ESPERA_MAXIMA),
        retry=retry_if_exception_type((FalhaTransitoriaError, httpx.TransportError)),
        before_sleep=_registrar_nova_tentativa,
        reraise=True,
    )


def _baixar_uma_vez(cliente: httpx.Client, arquivo: ArquivoRemoto, parcial: Path) -> None:
    ja_em_disco = parcial.stat().st_size if parcial.exists() else 0

    cabecalhos: dict[str, str] = {}
    if ja_em_disco and arquivo.etag:
        cabecalhos["Range"] = f"bytes={ja_em_disco}-"
        cabecalhos["If-Range"] = arquivo.etag

    with cliente.stream("GET", arquivo.caminho, headers=cabecalhos) as resposta:
        if resposta.status_code in _TRANSITORIOS:
            raise FalhaTransitoriaError(f"{arquivo.nome} devolveu {resposta.status_code}")
        resposta.raise_for_status()

        retomando = resposta.status_code == httpx.codes.PARTIAL_CONTENT
        if ja_em_disco and not retomando:
            logger.info(
                "servidor recusou a retomada; o arquivo mudou na origem e recomeça do zero",
                extra={"arquivo": arquivo.nome, "descartado_bytes": ja_em_disco},
            )

        with parcial.open("ab" if retomando else "wb") as saida:
            for bloco in resposta.iter_bytes(_BLOCO):
                saida.write(bloco)

    recebido = parcial.stat().st_size
    if arquivo.tamanho and recebido != arquivo.tamanho:
        parcial.unlink(missing_ok=True)
        raise FalhaTransitoriaError(
            f"{arquivo.nome} veio truncado: {recebido} bytes contra {arquivo.tamanho} anunciados"
        )


def baixar_arquivo(cliente: httpx.Client, arquivo: ArquivoRemoto, destino: Path) -> Path:
    """Baixa um arquivo em streaming, com retomada e nova tentativa.

    Escreve num `.parcial` e só renomeia ao fim. Assim o arquivo com nome
    definitivo nunca existe pela metade, e uma interrupção não deixa lixo que
    pareça um download bem-sucedido.
    """
    final = destino / arquivo.nome
    parcial = final.with_name(f"{arquivo.nome}.parcial")

    for tentativa in _politica_de_retentativa():
        with tentativa:
            _baixar_uma_vez(cliente, arquivo, parcial)
    parcial.replace(final)

    logger.info(
        "arquivo baixado",
        extra={"arquivo": arquivo.nome, "bytes": final.stat().st_size},
    )
    return final


def baixar_competencia(config: Config, competencia: str | None = None) -> list[Path]:
    """Baixa a competência inteira, validada antes de qualquer byte ser buscado."""
    alvo = competencia or config.competencia
    destino = config.data_dir / "bruto" / alvo
    destino.mkdir(parents=True, exist_ok=True)

    with criar_cliente(config) as cliente:
        arquivos = listar_arquivos(cliente, alvo)
        validar_competencia(alvo, arquivos)

        total = sum(arquivo.tamanho for arquivo in arquivos.values())
        logger.info(
            "competência validada, iniciando download",
            extra={"competencia": alvo, "arquivos": len(arquivos), "bytes_esperados": total},
        )
        return [baixar_arquivo(cliente, arquivos[nome], destino) for nome in sorted(arquivos)]
