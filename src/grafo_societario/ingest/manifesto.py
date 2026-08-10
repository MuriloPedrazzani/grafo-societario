"""Registro do que já foi baixado e verificado, por competência.

O manifesto é o que permite rodar o pipeline duas vezes sem baixar duas vezes.
Ele guarda apenas download **concluído e conferido**: arquivo parcial, truncado
ou interrompido não entra, porque uma entrada mentirosa aqui vira um arquivo
corrompido aceito como bom lá na frente.

A gravação é atômica — escreve num temporário, força o conteúdo para o disco e
só então renomeia. Um manifesto truncado por interrupção envenenaria o cache
inteiro, e o sintoma apareceria longe da causa: como erro de parsing no bronze,
dias depois, sem nada apontando para a ingestão.
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
from dataclasses import dataclass, field
from datetime import UTC, datetime
from enum import StrEnum
from pathlib import Path
from typing import Any, Final

logger = logging.getLogger(__name__)

NOME_DO_ARQUIVO: Final = "manifesto.json"
_BLOCO: Final = 1024 * 1024


class ModoDeVerificacao(StrEnum):
    """Quanto esforço gastar para decidir se o artefato em disco ainda serve."""

    RAPIDA = "rapida"
    """Compara apenas metadado. Não lê o conteúdo do disco."""

    COMPLETA = "completa"
    """Recalcula o SHA-256. Detecta corrupção que não muda o tamanho."""


def escrever_json_atomico(caminho: Path, conteudo: dict[str, Any]) -> Path:
    """Escreve num temporário, força ao disco e renomeia.

    Sem o fsync, o rename pode alcançar o disco antes do conteúdo, e uma queda de
    energia deixaria exatamente o arquivo vazio que a escrita atômica existe para
    impedir.
    """
    temporario = caminho.with_name(f"{caminho.name}.novo")
    with temporario.open("w", encoding="utf-8") as saida:
        json.dump(conteudo, saida, ensure_ascii=False, indent=2)
        saida.write("\n")
        saida.flush()
        os.fsync(saida.fileno())
    temporario.replace(caminho)
    return caminho


def ler_json(caminho: Path) -> dict[str, Any] | None:
    """Lê um manifesto, devolvendo `None` quando ausente ou ilegível.

    Cache ilegível deve custar retrabalho, não travar o pipeline.
    """
    if not caminho.exists():
        return None
    try:
        dados: dict[str, Any] = json.loads(caminho.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, UnicodeDecodeError) as erro:
        logger.warning(
            "manifesto ilegível foi descartado; o trabalho será refeito",
            extra={"caminho": str(caminho), "causa": str(erro)},
        )
        return None
    return dados


@dataclass(frozen=True)
class EntradaDoManifesto:
    """Um arquivo cujo download terminou e foi conferido."""

    nome: str
    tamanho: int
    sha256: str
    etag: str
    last_modified: str
    baixado_em: str

    @classmethod
    def agora(
        cls, nome: str, tamanho: int, sha256: str, etag: str, last_modified: str
    ) -> EntradaDoManifesto:
        return cls(
            nome=nome,
            tamanho=tamanho,
            sha256=sha256,
            etag=etag,
            last_modified=last_modified,
            baixado_em=datetime.now(tz=UTC).isoformat(timespec="seconds"),
        )


@dataclass
class Manifesto:
    """Conteúdo do manifesto de uma competência."""

    competencia: str
    entradas: dict[str, EntradaDoManifesto] = field(default_factory=dict)

    def registrar(self, entrada: EntradaDoManifesto) -> None:
        self.entradas[entrada.nome] = entrada


def calcular_sha256(caminho: Path) -> str:
    """Lê o arquivo do disco para conferir o hash.

    Caro por definição: só deve ser chamado na verificação sob demanda.
    """
    digestor = hashlib.sha256()
    with caminho.open("rb") as entrada:
        while bloco := entrada.read(_BLOCO):
            digestor.update(bloco)
    return digestor.hexdigest()


def caminho_do_manifesto(destino: Path) -> Path:
    return destino / NOME_DO_ARQUIVO


def carregar(destino: Path, competencia: str) -> Manifesto:
    """Lê o manifesto da competência.

    Manifesto ausente ou ilegível devolve registro vazio, e não exceção: o efeito
    é rebaixar tudo, que é lento mas correto. Falhar aqui deixaria o pipeline
    travado por causa de um arquivo de cache.
    """
    caminho = caminho_do_manifesto(destino)
    dados = ler_json(caminho)
    if dados is None:
        return Manifesto(competencia=competencia)

    try:
        entradas = {
            nome: EntradaDoManifesto(nome=nome, **campos)
            for nome, campos in dados["arquivos"].items()
        }
    except (KeyError, TypeError) as erro:
        logger.warning(
            "manifesto com formato inesperado foi descartado; a competência será rebaixada",
            extra={"caminho": str(caminho), "causa": str(erro)},
        )
        return Manifesto(competencia=competencia)

    return Manifesto(competencia=dados.get("competencia", competencia), entradas=entradas)


def gravar(manifesto: Manifesto, destino: Path) -> Path:
    """Grava o manifesto de forma atômica."""
    conteudo: dict[str, Any] = {
        "competencia": manifesto.competencia,
        "gerado_em": datetime.now(tz=UTC).isoformat(timespec="seconds"),
        "arquivos": {
            nome: {
                "tamanho": entrada.tamanho,
                "sha256": entrada.sha256,
                "etag": entrada.etag,
                "last_modified": entrada.last_modified,
                "baixado_em": entrada.baixado_em,
            }
            for nome, entrada in sorted(manifesto.entradas.items())
        },
    }
    return escrever_json_atomico(caminho_do_manifesto(destino), conteudo)
