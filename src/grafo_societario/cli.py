"""Interface de linha de comando — a porta canônica do pipeline.

Este módulo **orquestra e não trabalha**. Nenhuma regra de negócio mora aqui: a
lista de arquivos esperados, a validação da competência, a política de
retentativa e a checagem de espaço vivem nos módulos de ingestão. A CLI escolhe
a ordem, traduz falha em código de saída e decide como falar com quem está do
outro lado.

**Código de saída é contrato.** `0` só quando tudo terminou. Erro de uso ou de
configuração devolve `2`; falha de execução devolve `1`. Makefile e CI leem esse
número, e "falhou mas saiu 0" é o pior modo de falha que existe, porque
transforma erro em silêncio.

**A saída se adapta ao destino.** Barra de progresso serve a quem está olhando;
em log de CI ela vira uma linha ilegível cheia de retorno de carro. Quando a
saída não é um terminal, o andamento fica só no log estruturado, que é o formato
que uma máquina consegue ler.
"""

from __future__ import annotations

import logging
import sys
from typing import Annotated, Final

import typer

from grafo_societario import __version__
from grafo_societario.config import Config, ConfigInvalidaError, carregar_conexao, carregar_config
from grafo_societario.ingest import downloader, extract
from grafo_societario.ingest.manifesto import ModoDeVerificacao
from grafo_societario.logging_setup import configurar_logging

logger = logging.getLogger(__name__)

SUCESSO: Final = 0
FALHA: Final = 1
ERRO_DE_USO: Final = 2

app = typer.Typer(
    help="Pipeline do grafo societário a partir dos dados abertos de CNPJ.",
    no_args_is_help=True,
    add_completion=False,
)


class Progresso:
    """Mostra andamento só quando há gente olhando.

    Sem terminal, cada passo já está no log estruturado com `run_id`, competência
    e nome do arquivo — informação melhor que a barra, e legível por máquina.
    """

    def __init__(self, rotulo: str, *, ativo: bool) -> None:
        self.rotulo = rotulo
        self.ativo = ativo
        self._escreveu = False

    def __call__(self, nome: str, posicao: int, total: int) -> None:
        if not self.ativo:
            return
        sys.stderr.write(f"\r{self.rotulo} [{posicao:>2}/{total}] {nome:<34}")
        sys.stderr.flush()
        self._escreveu = True

    def encerrar(self) -> None:
        if self.ativo and self._escreveu:
            sys.stderr.write("\n")
            sys.stderr.flush()


def _ha_terminal() -> bool:
    return sys.stderr.isatty()


def _resolver_competencia(competencia: str | None, ultima: bool) -> str | None:
    """Traduz as opções numa competência concreta, ou deixa o ambiente decidir.

    Nunca escolhe sozinha: `--ultima` é pedido explícito, e o que ele resolveu
    aparece no log antes de qualquer download começar.
    """
    if not ultima:
        return competencia

    conexao = carregar_conexao()
    with downloader.criar_cliente(conexao) as cliente:
        escolhida = downloader.resolver_competencia_mais_recente(cliente)
    typer.echo(f"competência resolvida por --ultima: {escolhida}", err=True)
    return escolhida


@app.command()
def ingest(
    competencia: Annotated[
        str | None,
        typer.Option(help="Competência no formato AAAA-MM. Sem isto, vale COMPETENCIA."),
    ] = None,
    ultima: Annotated[
        bool,
        typer.Option(
            "--ultima",
            help="Resolve para a competência mais recente que esteja COMPLETA, e registra qual.",
        ),
    ] = False,
    verificar_integridade: Annotated[
        bool,
        typer.Option(
            "--verificar-integridade",
            help="Recalcula o SHA-256 do que está em disco. Correto e caro; padrão é conferir "
            "apenas tamanho e ETag.",
        ),
    ] = False,
    extrair: Annotated[
        bool,
        typer.Option("--extrair/--sem-extrair", help="Extrai os ZIPs após baixá-los."),
    ] = True,
) -> None:
    """Baixa e extrai os arquivos de uma competência da Receita Federal."""
    run_id = configurar_logging()
    modo = ModoDeVerificacao.COMPLETA if verificar_integridade else ModoDeVerificacao.RAPIDA
    mostrar = _ha_terminal()

    if competencia and ultima:
        typer.echo("Use --competencia ou --ultima, não os dois.", err=True)
        raise typer.Exit(ERRO_DE_USO)

    try:
        escolhida = _resolver_competencia(competencia, ultima)
        config = _config_de(escolhida)
    except ConfigInvalidaError as erro:
        typer.echo(str(erro), err=True)
        raise typer.Exit(ERRO_DE_USO) from erro
    except downloader.ErroDeIngestao as erro:
        typer.echo(str(erro), err=True)
        raise typer.Exit(FALHA) from erro

    logger.info(
        "ingestão iniciada",
        extra={
            "competencia": config.competencia,
            "verificacao": str(modo),
            "extrair": extrair,
            "versao": __version__,
        },
    )

    try:
        _executar(config, modo, extrair=extrair, mostrar=mostrar)
    except (downloader.ErroDeIngestao, extract.ErroDeExtracao, OSError) as erro:
        logger.exception("ingestão interrompida", extra={"competencia": config.competencia})
        typer.echo(f"\n{type(erro).__name__}: {erro}", err=True)
        raise typer.Exit(FALHA) from erro

    logger.info("ingestão concluída", extra={"competencia": config.competencia, "run_id": run_id})
    raise typer.Exit(SUCESSO)


def _config_de(competencia: str | None) -> Config:
    return carregar_config(competencia=competencia) if competencia else carregar_config()


def _executar(config: Config, modo: ModoDeVerificacao, *, extrair: bool, mostrar: bool) -> None:
    baixando = Progresso("baixando ", ativo=mostrar)
    try:
        baixados = downloader.baixar_competencia(config, modo=modo, ao_progredir=baixando)
    finally:
        baixando.encerrar()
    typer.echo(f"{len(baixados)} arquivos disponíveis em {config.data_dir / 'bruto'}", err=True)

    if not extrair:
        return

    extraindo = Progresso("extraindo", ativo=mostrar)
    try:
        extraidos = extract.extrair_competencia(config, modo=modo, ao_progredir=extraindo)
    finally:
        extraindo.encerrar()
    typer.echo(f"{len(extraidos)} arquivos extraídos em {config.data_dir / 'extraido'}", err=True)


@app.command()
def versao() -> None:
    """Mostra a versão do pacote."""
    typer.echo(__version__)


def main() -> None:
    app()
