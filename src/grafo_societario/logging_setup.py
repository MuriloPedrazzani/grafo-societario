"""Log estruturado em JSON, com um identificador único por execução.

O pipeline roda em etapas longas e sem ninguém olhando. Log em texto livre só
serve para ler na hora; em JSON, uma execução inteira pode ser filtrada,
agregada e comparada com a anterior depois do fato.

O `run_id` é o que costura as etapas: toda linha emitida durante uma execução
carrega o mesmo valor, então `ingest`, `bronze` e `silver` deixam de ser três
conjuntos de linhas soltas e viram uma execução com começo, meio e fim.

As chaves do JSON são protocolo, não domínio: ficam em inglês (`timestamp`,
`level`, `logger`, `message`, `exception`) porque quem consome isto é máquina, e
ferramenta de agregação espera a convenção. Os campos de negócio passados em
`extra=` seguem o vocabulário do domínio, em português.
"""

from __future__ import annotations

import json
import logging
import sys
import uuid
from contextvars import ContextVar
from datetime import UTC, datetime
from typing import IO, Any

_run_id: ContextVar[str] = ContextVar("run_id", default="")

# Atributos que o próprio logging põe em todo registro. O que sobrar veio de
# `extra=` e é campo de negócio, que merece ir para o JSON.
_ATRIBUTOS_INTERNOS = frozenset(
    vars(logging.LogRecord(name="", level=0, pathname="", lineno=0, msg="", args=(), exc_info=None))
) | {"asctime", "message", "taskName"}


def obter_run_id() -> str:
    """Identificador da execução corrente, ou vazio se o log não foi configurado."""
    return _run_id.get()


class FormatadorJson(logging.Formatter):
    """Serializa cada registro como um objeto JSON de uma linha."""

    def format(self, record: logging.LogRecord) -> str:
        dados: dict[str, Any] = {
            "timestamp": datetime.fromtimestamp(record.created, tz=UTC).isoformat(
                timespec="milliseconds"
            ),
            "level": record.levelname,
            "logger": record.name,
            "message": record.getMessage(),
            "run_id": obter_run_id(),
        }

        for chave, valor in record.__dict__.items():
            if chave not in _ATRIBUTOS_INTERNOS and not chave.startswith("_"):
                dados[chave] = valor

        if record.exc_info:
            dados["exception"] = self.formatException(record.exc_info)

        return json.dumps(dados, ensure_ascii=False, default=str)


def configurar_logging(
    nivel: int | str = logging.INFO,
    run_id: str | None = None,
    stream: IO[str] | None = None,
) -> str:
    """Instala o log JSON na raiz e devolve o `run_id` da execução.

    Chamar de novo substitui a configuração anterior em vez de acumular saídas
    duplicadas — importante porque a API e a CLI compartilham este módulo.
    """
    identificador = run_id or uuid.uuid4().hex[:12]
    _run_id.set(identificador)

    manipulador = logging.StreamHandler(stream if stream is not None else sys.stdout)
    manipulador.setFormatter(FormatadorJson())

    raiz = logging.getLogger()
    for anterior in list(raiz.handlers):
        raiz.removeHandler(anterior)
        anterior.close()
    raiz.addHandler(manipulador)
    raiz.setLevel(nivel)

    return identificador
