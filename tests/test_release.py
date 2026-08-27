"""O portão da Release: o que ele aceita, e sobretudo o que ele recusa.

O conferidor existe para reprovar. Testar só o caminho feliz seria o mesmo erro
da regra 4 do ESTADO — validação que nunca reprovou não provou que sabe reprovar
—, e aqui é pior que o normal, porque o modo de falha que ele previne é
silencioso: uma Release rotulada com a competência errada produz uma imagem que
sobe normalmente e serve o mês errado.

Cada recusa abaixo é um caso construído para falhar. O caminho feliz aparece uma
vez, e serve de controle para as recusas não estarem reprovando tudo.
"""

from __future__ import annotations

import ast
import gzip
import hashlib
import importlib.util
import io
import json
import sys
import tarfile
from pathlib import Path
from types import ModuleType

import pytest

RAIZ = Path(__file__).resolve().parents[1]

from grafo_societario.graph.artefatos import ARTEFATOS_PUBLICAVEIS  # noqa: E402


def _carregar(nome: str) -> ModuleType:
    """Importa um script de `scripts/`, que não é pacote instalável."""
    caminho = RAIZ / "scripts" / f"{nome}.py"
    especificacao = importlib.util.spec_from_file_location(nome, caminho)
    assert especificacao is not None and especificacao.loader is not None
    modulo = importlib.util.module_from_spec(especificacao)
    sys.modules[nome] = modulo
    especificacao.loader.exec_module(modulo)
    return modulo


empacotador = _carregar("empacotar_artefatos")
conferidor = _carregar("conferir_release")


@pytest.fixture
def artefatos_falsos(tmp_path: Path) -> Path:
    """Os 14 nomes declarados, com conteúdo mínimo e distinto.

    Conteúdo distinto por arquivo importa: com bytes iguais, trocar dois arquivos
    de lugar passaria despercebido, e o teste diria que a soma confere quando o
    que ela confere é outra coisa.
    """
    origem = tmp_path / "grafo" / "2026-06"
    origem.mkdir(parents=True)
    for indice, nome in enumerate(ARTEFATOS_PUBLICAVEIS):
        (origem / nome).write_bytes(f"conteudo de {nome} #{indice}".encode())
    return origem


@pytest.fixture
def pacote_valido(artefatos_falsos: Path, tmp_path: Path) -> tuple[Path, str]:
    destino = tmp_path / "dist"
    pacote = empacotador.montar(artefatos_falsos, "2026-06", "SP", destino)
    soma = hashlib.sha256(pacote.read_bytes()).hexdigest()
    return pacote, soma


def _remontar(pacote: Path, transformar) -> Path:  # type: ignore[no-untyped-def]
    """Reescreve o tar aplicando `transformar` à lista de (info, bytes).

    Escrito aqui, e não reaproveitado do empacotador, porque o objetivo é montar
    pacotes que o empacotador **nunca produziria** — é justamente isso que o
    conferidor precisa recusar.
    """
    with tarfile.open(pacote, "r:gz") as tar:
        membros = []
        for info in tar.getmembers():
            fluxo = tar.extractfile(info)
            membros.append((info, b"" if fluxo is None else fluxo.read()))

    novos = transformar(membros)
    saida = pacote.with_name("remontado.tar.gz")
    with (
        saida.open("wb") as bruto,
        gzip.GzipFile(fileobj=bruto, mode="wb", mtime=0) as gz,
        tarfile.open(fileobj=gz, mode="w") as tar,
    ):
        for info, corpo in novos:
            info.size = len(corpo)
            tar.addfile(info, io.BytesIO(corpo))
    return saida


def _soma(caminho: Path) -> str:
    return hashlib.sha256(caminho.read_bytes()).hexdigest()


# ------------------------------------------------------------- o caminho feliz


def test_o_pacote_bem_formado_passa(pacote_valido: tuple[Path, str]) -> None:
    pacote, soma = pacote_valido

    resumo = conferidor.conferir(pacote, "artefatos-2026-06", soma)

    assert resumo["competencia"] == "2026-06"
    assert resumo["arquivos"] == len(ARTEFATOS_PUBLICAVEIS)


# --------------------------------------------------------------- as recusas


def test_recusa_arquivo_a_mais(pacote_valido: tuple[Path, str]) -> None:
    """Sobra é tão grave quanto falta: um arquivo que este commit não declara
    publicável pode ser justamente o que a Fase 6 tirou do artefato — o
    identificador reversível de pessoa física."""
    pacote, _ = pacote_valido

    def acrescentar(membros):  # type: ignore[no-untyped-def]
        extra = tarfile.TarInfo("identificadores.parquet")
        extra.mtime = 0
        return [*membros, (extra, b"nao deveria estar aqui")]

    remontado = _remontar(pacote, acrescentar)

    with pytest.raises(conferidor.ReleaseInvalidaError, match="sobrando"):
        conferidor.conferir(remontado, "artefatos-2026-06", _soma(remontado))


def test_recusa_arquivo_a_menos(pacote_valido: tuple[Path, str]) -> None:
    pacote, _ = pacote_valido
    ausente = ARTEFATOS_PUBLICAVEIS[0]

    remontado = _remontar(
        pacote, lambda membros: [par for par in membros if par[0].name != ausente]
    )

    with pytest.raises(conferidor.ReleaseInvalidaError, match="faltando"):
        conferidor.conferir(remontado, "artefatos-2026-06", _soma(remontado))


def test_recusa_competencia_da_tag_diferente_da_de_dentro(
    pacote_valido: tuple[Path, str],
) -> None:
    """O modo de falha silencioso, e o motivo de o manifesto existir.

    A Release sai rotulada com um mês, o Dockerfile puxa por essa tag, e a imagem
    sobe servindo outro mês sem nada indicar que está errada.
    """
    pacote, soma = pacote_valido

    with pytest.raises(conferidor.ReleaseInvalidaError, match="rotulada errado"):
        conferidor.conferir(pacote, "artefatos-2026-07", soma)


def test_recusa_conteudo_adulterado(pacote_valido: tuple[Path, str]) -> None:
    pacote, _ = pacote_valido
    alvo = ARTEFATOS_PUBLICAVEIS[3]

    def adulterar(membros):  # type: ignore[no-untyped-def]
        return [
            (info, b"outro conteudo" if info.name == alvo else corpo) for info, corpo in membros
        ]

    remontado = _remontar(pacote, adulterar)

    with pytest.raises(conferidor.ReleaseInvalidaError, match="soma divergente"):
        conferidor.conferir(remontado, "artefatos-2026-06", _soma(remontado))


def test_recusa_soma_do_pacote_diferente_da_publicada(
    pacote_valido: tuple[Path, str],
) -> None:
    """Upload truncado e arquivo trocado: a máquina que empacotou não vê nenhum
    dos dois, porque os dois acontecem no transporte."""
    pacote, _ = pacote_valido

    with pytest.raises(conferidor.ReleaseInvalidaError, match="Upload truncado"):
        conferidor.conferir(pacote, "artefatos-2026-06", "0" * 64)


@pytest.mark.parametrize("tag", ["v1.0", "artefatos-2026", "artefatos-2026-6", "2026-06"])
def test_recusa_tag_fora_do_formato(pacote_valido: tuple[Path, str], tag: str) -> None:
    """O formato da tag é contrato com o Dockerfile, que monta a URL a partir
    dele. Tag fora do padrão não é questão de estilo: é URL que não resolve."""
    pacote, soma = pacote_valido

    with pytest.raises(conferidor.ReleaseInvalidaError, match="fora do formato"):
        conferidor.conferir(pacote, tag, soma)


# ------------------------------------------------- propriedades do empacotador


def test_o_pacote_e_reprodutivel(artefatos_falsos: Path, tmp_path: Path) -> None:
    """Mesmo conteúdo, mesma soma. Sem isso a soma significaria "esta execução"
    em vez de "este conteúdo", e o `.sha256` perderia o sentido."""
    primeiro = empacotador.montar(artefatos_falsos, "2026-06", "SP", tmp_path / "a")
    segundo = empacotador.montar(artefatos_falsos, "2026-06", "SP", tmp_path / "b")

    assert _soma(primeiro) == _soma(segundo)


def test_o_empacotador_recusa_construir_com_artefato_faltando(
    artefatos_falsos: Path, tmp_path: Path
) -> None:
    """Ele não constrói nada: se falta artefato, o erro é de quem não construiu,
    e adivinhar aqui produziria uma Release incompleta com aparência de completa."""
    (artefatos_falsos / ARTEFATOS_PUBLICAVEIS[2]).unlink()

    with pytest.raises(SystemExit, match="faltam 1 artefatos"):
        empacotador.montar(artefatos_falsos, "2026-06", "SP", tmp_path / "c")


def test_o_manifesto_e_o_unico_membro_que_nao_e_artefato(
    pacote_valido: tuple[Path, str],
) -> None:
    pacote, _ = pacote_valido

    with tarfile.open(pacote, "r:gz") as tar:
        nomes = set(tar.getnames())
        manifesto = json.loads(tar.extractfile("manifesto.json").read().decode("utf-8"))  # type: ignore[union-attr]

    assert nomes - set(ARTEFATOS_PUBLICAVEIS) == {"manifesto.json"}
    assert manifesto["competencia"] == "2026-06"
    assert set(manifesto["arquivos"]) == set(ARTEFATOS_PUBLICAVEIS)


def test_montador_e_conferidor_nao_compartilham_logica() -> None:
    """A precisão que motiva os dois arquivos: se o mesmo código monta e confere,
    a conferência não pega defeito do montador — ela confirma que ele foi
    consistente consigo mesmo.

    O que as duas metades podem ter em comum é `ARTEFATOS_PUBLICAVEIS`, e só.

    Procurar por texto não serviria: os dois arquivos **citam** um ao outro na
    prosa, explicando por que são separados. Quem publica a lista real de imports
    é a própria AST, e perguntar a ela é mais barato e mais exato que inventar um
    detector — corolário da regra 14 do ESTADO.
    """
    importados = {}
    for nome in ("conferir_release", "empacotar_artefatos"):
        arvore = ast.parse((RAIZ / "scripts" / f"{nome}.py").read_text(encoding="utf-8"))
        modulos: set[str] = set()
        for no in ast.walk(arvore):
            if isinstance(no, ast.Import):
                modulos.update(alias.name for alias in no.names)
            elif isinstance(no, ast.ImportFrom) and no.module:
                modulos.add(no.module)
        importados[nome] = modulos

    assert "empacotar_artefatos" not in importados["conferir_release"], (
        "o conferidor importou o montador; a conferência deixou de ser independente"
    )
    assert "conferir_release" not in importados["empacotar_artefatos"]

    comum = importados["conferir_release"] & importados["empacotar_artefatos"]
    do_projeto = {modulo for modulo in comum if modulo.startswith("grafo_societario")}
    assert do_projeto == {"grafo_societario.graph.artefatos"}, (
        f"as duas metades só podem se encontrar em ARTEFATOS_PUBLICAVEIS; "
        f"compartilham também {do_projeto - {'grafo_societario.graph.artefatos'}}"
    )
