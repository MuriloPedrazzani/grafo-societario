"""Configuração do pipeline, lida de variáveis de ambiente.

Uma única fonte de verdade para os parâmetros que mudam entre execuções. Erro de
configuração é traduzido para uma mensagem que diz qual variável está errada e
onde consultar o valor esperado — a falha acontece no início, não no meio de um
processamento de dezenas de minutos.
"""

from __future__ import annotations

import re
from pathlib import Path

from pydantic import ValidationError, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

UFS_VALIDAS = frozenset(
    [
        "AC",
        "AL",
        "AM",
        "AP",
        "BA",
        "CE",
        "DF",
        "ES",
        "GO",
        "MA",
        "MG",
        "MS",
        "MT",
        "PA",
        "PB",
        "PE",
        "PI",
        "PR",
        "RJ",
        "RN",
        "RO",
        "RR",
        "RS",
        "SC",
        "SE",
        "SP",
        "TO",
    ]
)

_COMPETENCIA = re.compile(r"^\d{4}-(0[1-9]|1[0-2])$")


class ConfigInvalidaError(RuntimeError):
    """Configuração ausente ou malformada, já traduzida para quem executa."""


class Config(BaseSettings):
    """Parâmetros de execução do pipeline."""

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        case_sensitive=False,
        extra="ignore",
    )

    competencia: str
    """Competência dos dados da Receita Federal, no formato AAAA-MM."""

    uf_alvo: str = "SP"
    """UF do estabelecimento matriz usada no recorte."""

    data_dir: Path = Path("data")
    """Raiz onde os dados baixados e processados são gravados."""

    @field_validator("uf_alvo")
    @classmethod
    def _uf_conhecida(cls, valor: str) -> str:
        uf = valor.strip().upper()
        if uf not in UFS_VALIDAS:
            raise ValueError(f"{valor!r} não é uma sigla de UF. Use uma das 27, por exemplo SP.")
        return uf

    @field_validator("competencia")
    @classmethod
    def _competencia_bem_formada(cls, valor: str) -> str:
        competencia = valor.strip()
        if not _COMPETENCIA.match(competencia):
            raise ValueError(f"{valor!r} não está no formato AAAA-MM, por exemplo 2026-06.")
        return competencia


def _mensagem_amigavel(erro: ValidationError) -> str:
    linhas = ["Configuração inválida. Corrija as variáveis de ambiente abaixo:"]
    for detalhe in erro.errors():
        variavel = str(detalhe["loc"][0]).upper() if detalhe["loc"] else "(desconhecida)"
        if detalhe["type"] == "missing":
            linhas.append(f"  {variavel}: obrigatória, e não foi definida.")
        else:
            linhas.append(f"  {variavel}: {detalhe['msg'].removeprefix('Value error, ')}")
    linhas.append("Os valores esperados estão em .env.example.")
    return "\n".join(linhas)


def carregar_config(env_file: str | Path | None = ".env") -> Config:
    """Lê a configuração do ambiente.

    Levanta `ConfigInvalidaError` com uma mensagem legível quando algo falta ou
    está malformado. Passar `env_file=None` ignora o arquivo `.env` e lê apenas
    do ambiente do processo.
    """
    try:
        # O __init__ que o pydantic sintetiza lista apenas os campos do modelo, então
        # o mypy não reconhece `_env_file`, que a própria pydantic-settings aceita e
        # documenta. O silenciamento é de uma linha, e `warn_unused_ignores` o remove
        # automaticamente no dia em que a biblioteca passar a tipar esse argumento.
        return Config(_env_file=env_file)  # type: ignore[call-arg]
    except ValidationError as erro:
        raise ConfigInvalidaError(_mensagem_amigavel(erro)) from erro
