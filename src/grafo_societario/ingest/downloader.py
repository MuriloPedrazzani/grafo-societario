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

import hashlib
import logging
from collections.abc import Callable
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

from grafo_societario.config import ConexaoRfb, Config
from grafo_societario.ingest import manifesto
from grafo_societario.ingest.manifesto import EntradaDoManifesto, ModoDeVerificacao

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
    last_modified: str


@dataclass(frozen=True)
class ArquivoBaixado:
    """Resultado de um download concluído, com o hash obtido no mesmo passe."""

    caminho: Path
    sha256: str


def resolver_competencia_mais_recente(cliente: httpx.Client) -> str:
    """Devolve a competência mais recente que está completa.

    Percorre da mais nova para a mais antiga porque a última publicada pode estar
    em pleno envio. Cada candidata descartada é registrada no log, e a escolhida
    também: uma competência escolhida em silêncio é uma competência que ninguém
    vai conferir.
    """
    for competencia in reversed(listar_competencias(cliente)):
        faltando = ARQUIVOS_ESPERADOS - listar_arquivos(cliente, competencia).keys()
        if not faltando:
            logger.info(
                "competência mais recente completa foi escolhida",
                extra={"competencia": competencia},
            )
            return competencia
        logger.info(
            "competência descartada por estar incompleta",
            extra={"competencia": competencia, "faltando": len(faltando)},
        )

    raise CompetenciaIncompletaError(
        "Nenhuma competência do compartilhamento está completa. "
        "Ou a origem mudou de formato, ou a publicação está em andamento."
    )


def criar_cliente(config: ConexaoRfb) -> httpx.Client:
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
            last_modified=prop.findtext(f"{_DAV}getlastmodified") or "",
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


def _semear_com_o_parcial(digestor: hashlib._Hash, parcial: Path, ate: int) -> None:
    """Alimenta o digestor com os bytes já em disco, ao retomar um download.

    É a única leitura extra que existe, e só acontece quando houve interrupção.
    No caminho normal o hash sai no mesmo passe dos bytes que chegam da rede.
    """
    lidos = 0
    with parcial.open("rb") as entrada:
        while lidos < ate and (bloco := entrada.read(min(_BLOCO, ate - lidos))):
            digestor.update(bloco)
            lidos += len(bloco)


def _baixar_uma_vez(cliente: httpx.Client, arquivo: ArquivoRemoto, parcial: Path) -> str:
    ja_em_disco = parcial.stat().st_size if parcial.exists() else 0

    cabecalhos: dict[str, str] = {}
    if ja_em_disco and arquivo.etag:
        cabecalhos["Range"] = f"bytes={ja_em_disco}-"
        cabecalhos["If-Range"] = arquivo.etag

    digestor = hashlib.sha256()

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
        if retomando:
            _semear_com_o_parcial(digestor, parcial, ja_em_disco)

        with parcial.open("ab" if retomando else "wb") as saida:
            for bloco in resposta.iter_bytes(_BLOCO):
                saida.write(bloco)
                digestor.update(bloco)

    recebido = parcial.stat().st_size
    if arquivo.tamanho and recebido != arquivo.tamanho:
        parcial.unlink(missing_ok=True)
        raise FalhaTransitoriaError(
            f"{arquivo.nome} veio truncado: {recebido} bytes contra {arquivo.tamanho} anunciados"
        )
    return digestor.hexdigest()


def baixar_arquivo(cliente: httpx.Client, arquivo: ArquivoRemoto, destino: Path) -> ArquivoBaixado:
    """Baixa um arquivo em streaming, com retomada e nova tentativa.

    Escreve num `.parcial` e só renomeia ao fim. Assim o arquivo com nome
    definitivo nunca existe pela metade, e uma interrupção não deixa lixo que
    pareça um download bem-sucedido. O SHA-256 sai junto, sem releitura.
    """
    final = destino / arquivo.nome
    parcial = final.with_name(f"{arquivo.nome}.parcial")

    sha256 = ""
    for tentativa in _politica_de_retentativa():
        with tentativa:
            sha256 = _baixar_uma_vez(cliente, arquivo, parcial)
    parcial.replace(final)

    logger.info(
        "arquivo baixado",
        extra={"arquivo": arquivo.nome, "bytes": final.stat().st_size, "sha256": sha256},
    )
    return ArquivoBaixado(caminho=final, sha256=sha256)


def _ja_serve_o_que_esta_em_disco(
    arquivo: ArquivoRemoto,
    entrada: EntradaDoManifesto | None,
    destino: Path,
    modo: ModoDeVerificacao,
) -> bool:
    """Decide se dá para reaproveitar o arquivo local.

    A verificação rápida usa só o que a listagem já trouxe — tamanho e ETag — e
    não toca no conteúdo do disco. Reler 6,79 GiB para confirmar o que não mudou
    custaria, em toda execução, quase o mesmo que baixar de novo.
    """
    if entrada is None:
        return False

    local = destino / arquivo.nome
    if not local.exists():
        return False
    if entrada.tamanho != arquivo.tamanho or local.stat().st_size != arquivo.tamanho:
        return False
    if entrada.etag != arquivo.etag:
        return False

    if modo is ModoDeVerificacao.COMPLETA and manifesto.calcular_sha256(local) != entrada.sha256:
        logger.warning(
            "conteúdo local diverge do hash registrado; o arquivo será baixado de novo",
            extra={"arquivo": arquivo.nome, "sha256_esperado": entrada.sha256},
        )
        return False

    return True


def baixar_competencia(
    config: Config,
    competencia: str | None = None,
    modo: ModoDeVerificacao = ModoDeVerificacao.RAPIDA,
    ao_progredir: Callable[[str, int, int], None] | None = None,
) -> list[Path]:
    """Baixa a competência, reaproveitando o que o manifesto já garante.

    O manifesto é regravado a cada arquivo concluído. Uma interrupção no meio
    preserva o que já terminou, em vez de jogar fora horas de download por causa
    de um registro que só existia em memória.
    """
    alvo = competencia or config.competencia
    destino = config.data_dir / "bruto" / alvo
    destino.mkdir(parents=True, exist_ok=True)
    registro = manifesto.carregar(destino, alvo)

    caminhos: list[Path] = []
    reaproveitados = 0

    with criar_cliente(config) as cliente:
        arquivos = listar_arquivos(cliente, alvo)
        validar_competencia(alvo, arquivos)

        total = sum(arquivo.tamanho for arquivo in arquivos.values())
        logger.info(
            "competência validada",
            extra={
                "competencia": alvo,
                "arquivos": len(arquivos),
                "bytes_esperados": total,
                "verificacao": str(modo),
            },
        )

        for posicao, nome in enumerate(sorted(arquivos), start=1):
            if ao_progredir is not None:
                ao_progredir(nome, posicao, len(arquivos))
            remoto = arquivos[nome]
            if _ja_serve_o_que_esta_em_disco(remoto, registro.entradas.get(nome), destino, modo):
                reaproveitados += 1
                caminhos.append(destino / nome)
                continue

            baixado = baixar_arquivo(cliente, remoto, destino)
            registro.registrar(
                EntradaDoManifesto.agora(
                    nome=nome,
                    tamanho=remoto.tamanho,
                    sha256=baixado.sha256,
                    etag=remoto.etag,
                    last_modified=remoto.last_modified,
                )
            )
            manifesto.gravar(registro, destino)
            caminhos.append(baixado.caminho)

    logger.info(
        "competência pronta",
        extra={
            "competencia": alvo,
            "reaproveitados": reaproveitados,
            "baixados": len(caminhos) - reaproveitados,
        },
    )
    return caminhos
