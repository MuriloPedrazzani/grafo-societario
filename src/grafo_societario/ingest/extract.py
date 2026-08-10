"""Extração dos ZIPs da Receita Federal, em streaming e com espaço medido antes.

Quatro propriedades sustentam este módulo:

**Mede antes de começar.** Os 6,79 GiB comprimidos de uma competência viram
23,24 GiB em disco — razão de 3,42x, medida no diretório central dos próprios
arquivos. Descobrir que falta espaço no arquivo 28 de 36 desperdiça meia hora e
deixa o destino sujo. A conta é feita primeiro, e a falha traz o número exato que
falta.

**Nunca carrega um membro inteiro.** O maior membro da competência tem 6,31 GiB
descomprimidos, sozinho maior que a folga de RAM da máquina alvo. A leitura é
bloco a bloco, do começo ao fim.

**Nome de membro é dado hostil.** Um ZIP pode declarar `../../algo` ou caminho
absoluto e escrever fora do destino — o chamado zip-slip. O nome que vem do
arquivo nunca vira caminho aqui, e ainda assim é validado: um ZIP com nome
perigoso é recusado inteiro, porque nome assim não acontece por acaso.

**O nome de saída é previsível, a origem fica registrada.** Os membros se chamam
`K3241.K03200Y0.D60613.ESTABELE`, com a competência embutida no nome — casar isso
por expressão regular obrigaria a mudar o padrão todo mês. A saída vira
`Estabelecimentos0.csv`, e o nome original é preservado no manifesto, onde serve
à rastreabilidade sem contaminar o resto do pipeline.
"""

from __future__ import annotations

import hashlib
import logging
import shutil
import zipfile
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path, PurePosixPath, PureWindowsPath
from typing import Any, Final

from grafo_societario.config import Config
from grafo_societario.ingest import manifesto
from grafo_societario.ingest.manifesto import ModoDeVerificacao

logger = logging.getLogger(__name__)

NOME_DO_MANIFESTO: Final = "manifesto-extracao.json"
_BLOCO: Final = 1024 * 1024


class ErroDeExtracao(RuntimeError):
    """Falha ao extrair os arquivos da Receita Federal."""


class ExtracaoInseguraError(ErroDeExtracao):
    """O ZIP declara um membro cujo nome escaparia do diretório de destino."""


class EspacoInsuficienteError(ErroDeExtracao):
    """Não há espaço em disco para o conteúdo descomprimido."""


@dataclass(frozen=True)
class EntradaExtraida:
    """Um arquivo extraído, com a origem preservada."""

    nome: str
    origem: str
    membro_original: str
    tamanho: int
    sha256: str
    extraido_em: str


@dataclass
class ManifestoDeExtracao:
    competencia: str
    entradas: dict[str, EntradaExtraida] = field(default_factory=dict)

    def registrar(self, entrada: EntradaExtraida) -> None:
        self.entradas[entrada.nome] = entrada


def carregar_manifesto(destino: Path, competencia: str) -> ManifestoDeExtracao:
    dados = manifesto.ler_json(destino / NOME_DO_MANIFESTO)
    if dados is None:
        return ManifestoDeExtracao(competencia=competencia)
    try:
        entradas = {
            nome: EntradaExtraida(nome=nome, **campos) for nome, campos in dados["arquivos"].items()
        }
    except (KeyError, TypeError) as erro:
        logger.warning(
            "manifesto de extração com formato inesperado foi descartado",
            extra={"competencia": competencia, "causa": str(erro)},
        )
        return ManifestoDeExtracao(competencia=competencia)
    return ManifestoDeExtracao(competencia=dados.get("competencia", competencia), entradas=entradas)


def gravar_manifesto(registro: ManifestoDeExtracao, destino: Path) -> Path:
    conteudo: dict[str, Any] = {
        "competencia": registro.competencia,
        "gerado_em": datetime.now(tz=UTC).isoformat(timespec="seconds"),
        "arquivos": {
            nome: {
                "origem": entrada.origem,
                "membro_original": entrada.membro_original,
                "tamanho": entrada.tamanho,
                "sha256": entrada.sha256,
                "extraido_em": entrada.extraido_em,
            }
            for nome, entrada in sorted(registro.entradas.items())
        },
    }
    return manifesto.escrever_json_atomico(destino / NOME_DO_MANIFESTO, conteudo)


def recusar_nome_perigoso(origem: Path, nome: str) -> None:
    """Recusa nome de membro que escaparia do destino.

    O `zipfile` sanitiza em `extract()`, mas não em `open()` — e aqui a leitura é
    manual, justamente para poder ser feita em streaming. A validação, portanto,
    é responsabilidade deste módulo, não da biblioteca.
    """
    posix = PurePosixPath(nome)
    windows = PureWindowsPath(nome)
    perigoso = (
        posix.is_absolute()
        or windows.is_absolute()
        or windows.drive != ""
        or ".." in posix.parts
        or ".." in windows.parts
    )
    if perigoso:
        raise ExtracaoInseguraError(
            f"{origem.name} declara o membro {nome!r}, que escaparia do diretório de "
            "destino. O arquivo inteiro foi recusado: nome assim não acontece por acaso."
        )


def _confirmar_dentro_do_destino(destino: Path, candidato: Path) -> None:
    """Segunda camada: confere o caminho já resolvido, com links simbólicos seguidos."""
    if not candidato.resolve().is_relative_to(destino.resolve()):
        raise ExtracaoInseguraError(
            f"{candidato} ficaria fora de {destino} depois de resolvido o caminho."
        )


def nome_de_saida(origem: Path, indice: int, total: int) -> str:
    """Nome previsível: `Estabelecimentos0.zip` vira `Estabelecimentos0.csv`."""
    if total == 1:
        return f"{origem.stem}.csv"
    return f"{origem.stem}-{indice + 1}.csv"


def medir_descomprimido(zips: list[Path]) -> dict[Path, int]:
    """Soma o tamanho declarado no diretório central, sem descomprimir nada."""
    medidas: dict[Path, int] = {}
    for caminho in zips:
        with zipfile.ZipFile(caminho) as arquivo:
            for membro in arquivo.infolist():
                recusar_nome_perigoso(caminho, membro.filename)
            medidas[caminho] = sum(m.file_size for m in arquivo.infolist() if not m.is_dir())
    return medidas


def verificar_espaco(destino: Path, necessario: int) -> None:
    """Falha antes de começar quando o disco não comporta a extração."""
    livre = shutil.disk_usage(destino).free
    if necessario <= livre:
        logger.info(
            "espaço conferido antes da extração",
            extra={
                "necessario_bytes": necessario,
                "livre_bytes": livre,
                "folga_bytes": livre - necessario,
            },
        )
        return

    faltam = necessario - livre
    raise EspacoInsuficienteError(
        f"A extração precisa de {necessario / 1024**3:.2f} GiB e há "
        f"{livre / 1024**3:.2f} GiB livres em {destino}. "
        f"Faltam {faltam / 1024**3:.2f} GiB ({faltam:,} bytes). "
        "Libere espaço, aponte DATA_DIR para outro disco, ou desligue MANTER_ZIP "
        "para descartar cada ZIP após extraí-lo."
    )


def _ja_serve_o_que_esta_em_disco(
    destino: Path, entrada: EntradaExtraida | None, modo: ModoDeVerificacao
) -> bool:
    if entrada is None:
        return False
    local = destino / entrada.nome
    if not local.exists() or local.stat().st_size != entrada.tamanho:
        return False
    if modo is ModoDeVerificacao.COMPLETA and manifesto.calcular_sha256(local) != entrada.sha256:
        logger.warning(
            "arquivo extraído diverge do hash registrado; será extraído de novo",
            extra={"arquivo": entrada.nome},
        )
        return False
    return True


def _falta_extrair(
    registro: ManifestoDeExtracao, destino: Path, origem: Path, modo: ModoDeVerificacao
) -> bool:
    entradas = [entrada for entrada in registro.entradas.values() if entrada.origem == origem.name]
    if not entradas:
        return True
    return not all(_ja_serve_o_que_esta_em_disco(destino, entrada, modo) for entrada in entradas)


def _extrair_membro(
    arquivo: zipfile.ZipFile, membro: zipfile.ZipInfo, alvo: Path
) -> tuple[int, str]:
    """Copia um membro em blocos, calculando o hash no mesmo passe."""
    parcial = alvo.with_name(f"{alvo.name}.parcial")
    digestor = hashlib.sha256()
    escritos = 0

    with arquivo.open(membro) as origem, parcial.open("wb") as saida:
        while bloco := origem.read(_BLOCO):
            saida.write(bloco)
            digestor.update(bloco)
            escritos += len(bloco)

    if membro.file_size and escritos != membro.file_size:
        parcial.unlink(missing_ok=True)
        raise ErroDeExtracao(
            f"{membro.filename} rendeu {escritos} bytes contra {membro.file_size} declarados"
        )

    parcial.replace(alvo)
    return escritos, digestor.hexdigest()


def extrair_arquivo(
    origem: Path, destino: Path, registro: ManifestoDeExtracao, modo: ModoDeVerificacao
) -> list[Path]:
    """Extrai um ZIP, pulando o que o manifesto já garante."""
    extraidos: list[Path] = []

    with zipfile.ZipFile(origem) as arquivo:
        membros = [membro for membro in arquivo.infolist() if not membro.is_dir()]
        for indice, membro in enumerate(membros):
            recusar_nome_perigoso(origem, membro.filename)
            nome = nome_de_saida(origem, indice, len(membros))
            alvo = destino / nome
            _confirmar_dentro_do_destino(destino, alvo)

            if _ja_serve_o_que_esta_em_disco(destino, registro.entradas.get(nome), modo):
                extraidos.append(alvo)
                continue

            tamanho, sha256 = _extrair_membro(arquivo, membro, alvo)
            registro.registrar(
                EntradaExtraida(
                    nome=nome,
                    origem=origem.name,
                    membro_original=membro.filename,
                    tamanho=tamanho,
                    sha256=sha256,
                    extraido_em=datetime.now(tz=UTC).isoformat(timespec="seconds"),
                )
            )
            gravar_manifesto(registro, destino)
            logger.info(
                "membro extraído",
                extra={
                    "origem": origem.name,
                    "membro_original": membro.filename,
                    "arquivo": nome,
                    "bytes": tamanho,
                },
            )
            extraidos.append(alvo)

    return extraidos


def extrair_competencia(
    config: Config,
    competencia: str | None = None,
    modo: ModoDeVerificacao = ModoDeVerificacao.RAPIDA,
    ao_progredir: Callable[[str, int, int], None] | None = None,
) -> list[Path]:
    """Extrai todos os ZIPs baixados de uma competência."""
    alvo = competencia or config.competencia
    entrada = config.data_dir / "bruto" / alvo
    destino = config.data_dir / "extraido" / alvo
    destino.mkdir(parents=True, exist_ok=True)

    zips = sorted(entrada.glob("*.zip"))
    if not zips:
        raise ErroDeExtracao(f"Nenhum ZIP em {entrada}. Rode a aquisição antes da extração.")

    registro = carregar_manifesto(destino, alvo)
    medidas = medir_descomprimido(zips)

    # O espaço é conferido só contra o que ainda falta extrair: exigir os 23,24 GiB
    # inteiros numa reexecução recusaria um disco que já contém o resultado.
    pendentes = [caminho for caminho in zips if _falta_extrair(registro, destino, caminho, modo)]
    verificar_espaco(destino, sum(medidas[caminho] for caminho in pendentes))

    logger.info(
        "extração iniciada",
        extra={
            "competencia": alvo,
            "zips": len(zips),
            "pendentes": len(pendentes),
            "descomprimido_bytes": sum(medidas.values()),
        },
    )

    extraidos: list[Path] = []
    for posicao, caminho in enumerate(zips, start=1):
        if ao_progredir is not None:
            ao_progredir(caminho.name, posicao, len(zips))
        extraidos.extend(extrair_arquivo(caminho, destino, registro, modo))
        if not config.manter_zip:
            caminho.unlink()
            logger.info("ZIP descartado após a extração", extra={"arquivo": caminho.name})

    logger.info(
        "extração concluída",
        extra={"competencia": alvo, "arquivos": len(extraidos)},
    )
    return extraidos
