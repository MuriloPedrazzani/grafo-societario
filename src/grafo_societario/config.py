"""Configuração do pipeline, lida de variáveis de ambiente.

Uma única fonte de verdade para os parâmetros que mudam entre execuções. Erro de
configuração é traduzido para uma mensagem que diz qual variável está errada e
onde consultar o valor esperado — a falha acontece no início, não no meio de um
processamento de dezenas de minutos.
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

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


class ConexaoRfb(BaseSettings):
    """Só o necessário para falar com o compartilhamento da Receita Federal.

    Existe separado porque descobrir qual é a competência mais recente exige
    conversar com o servidor **antes** de haver competência escolhida. Sem esta
    separação, seria preciso inventar um valor de mentira só para construir a
    configuração — e valor de mentira acaba vazando para log e artefato.
    """

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        case_sensitive=False,
        extra="ignore",
    )

    rfb_url_base: str = "https://arquivos.receitafederal.gov.br"
    """Host do compartilhamento público da Receita Federal."""

    rfb_token_compartilhamento: str = "YggdBLfdninEJX9"
    """Token do compartilhamento. Tem padrão embutido de propósito: exigir
    configuração aqui quebraria a reprodução por quem apenas clona o repositório.
    Se a Receita rotacionar o compartilhamento, muda a variável, não o código."""

    @field_validator("rfb_url_base")
    @classmethod
    def _url_utilizavel(cls, valor: str) -> str:
        url = valor.strip().rstrip("/")
        partes = urlsplit(url)
        if partes.scheme == "https":
            return url
        if partes.scheme == "http" and partes.hostname in {"localhost", "127.0.0.1", "::1"}:
            return url
        raise ValueError(
            f"{valor!r} precisa usar https://. A única exceção é servidor local de teste."
        )

    @field_validator("rfb_token_compartilhamento")
    @classmethod
    def _token_preenchido(cls, valor: str) -> str:
        token = valor.strip()
        if not token:
            raise ValueError("não pode ser vazio; deixe em branco para usar o padrão embutido.")
        return token


class Config(ConexaoRfb):
    """Parâmetros de execução do pipeline."""

    competencia: str
    """Competência dos dados da Receita Federal, no formato AAAA-MM."""

    uf_alvo: str = "SP"
    """UF do estabelecimento matriz usada no recorte."""

    data_dir: Path = Path("data")
    """Raiz onde os dados baixados e processados são gravados."""

    limite_de_memoria: str = "4GB"
    """Teto de memória do motor de ETL. O projeto promete rodar em 8 GiB, e numa
    máquina de 8 GiB reais sobram cinco a seis depois do sistema — o processo
    Python ainda ocupa parte disso. Quatro deixa folga para os dois e mantém a
    promessa verdadeira na máquina que ela descreve, não só na de quem a escreveu."""

    manter_zip: bool = True
    """Preserva o ZIP depois de extrair. Descartar libera disco, mas destrói o
    cache de download: a execução seguinte rebaixaria 6,79 GiB da Receita para
    economizar espaço que já estava pago."""

    expor_pf: bool = False
    """Se o **nome** de pessoa física entra nos artefatos do grafo.

    Falso por padrão, e é assim que o artefato publicado é construído. Verdadeiro
    é para quem roda o pipeline localmente sobre os dados originais e precisa dos
    nomes — o código é aberto justamente para isso.

    A flag decide na **geração**, não na resposta da API, pelo mesmo motivo que
    moveu a supressão de CPF para a transformação: os artefatos vão para GitHub
    Release e para imagem Docker, e nome que entrou no artefato já saiu. Filtrar
    depois não desfaz publicação."""

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


def carregar_conexao(env_file: str | Path | None = ".env") -> ConexaoRfb:
    """Lê apenas o necessário para alcançar o compartilhamento da Receita."""
    try:
        return ConexaoRfb(_env_file=env_file)  # type: ignore[call-arg]
    except ValidationError as erro:
        raise ConfigInvalidaError(_mensagem_amigavel(erro)) from erro


def carregar_config(env_file: str | Path | None = ".env", **sobrescritas: Any) -> Config:
    """Lê a configuração do ambiente.

    Levanta `ConfigInvalidaError` com uma mensagem legível quando algo falta ou
    está malformado. Passar `env_file=None` ignora o arquivo `.env` e lê apenas
    do ambiente do processo. As sobrescritas têm prioridade sobre ambiente e
    `.env`, e existem para que a linha de comando possa mandar mais que eles.
    """
    try:
        # O __init__ que o pydantic sintetiza lista apenas os campos do modelo, então
        # o mypy não reconhece `_env_file`, que a própria pydantic-settings aceita e
        # documenta. O silenciamento é de uma linha, e `warn_unused_ignores` o remove
        # automaticamente no dia em que a biblioteca passar a tipar esse argumento.
        return Config(_env_file=env_file, **sobrescritas)  # type: ignore[call-arg]
    except ValidationError as erro:
        raise ConfigInvalidaError(_mensagem_amigavel(erro)) from erro
