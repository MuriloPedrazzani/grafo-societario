"""Nenhum CPF real no repositório — nem em código, nem em teste, nem em documento.

Este projeto suprime documento dos artefatos que publica. Publicá-lo no próprio
código-fonte seria a contradição mais cara possível, e ela quase aconteceu: a
primeira versão dos testes travava sete pessoas físicas nominalmente, com nome e
máscara reais, permanentemente no histórico de um repositório público.

Corrigir uma vez não basta. O commit seguinte reintroduz sem querer, porque colar
o valor observado é o caminho natural quando se está depurando. Este teste é o que
transforma a correção em regra: um CPF válido só pode aparecer se estiver
declarado abaixo como sintético.

A fixture tem guarda própria em `test_fixtures.py`, mais antiga e mais estrita —
lá nenhum número de onze dígitos pode validar, porque nada na fixture precisa
disso.
"""

from __future__ import annotations

import re
import subprocess
from pathlib import Path

import pytest

from test_fixtures import cpf_valido

RAIZ = Path(__file__).resolve().parent.parent

SINTETICOS = frozenset(
    {
        "11144477735",
        "22255588846",
        "01234567890",
        "00123456797",
    }
)
"""CPFs sintéticos, gerados de bases artificiais — `111444777`, `222555888`,
`012345678`, `001234567` — para exercitar o dígito verificador.

Eles precisam validar: as cláusulas de supressão por verificador não têm como ser
testadas com número inválido. O que eles não podem ser é observado no dado. Cada
um cobre uma forma: sem zero à esquerda, para escrever como nove mais dois, com um
zero perdido e com dois.

**Acrescentar um valor aqui é uma decisão, e é para ser.** Se o número veio do
arquivo da Receita, ele não entra — gere outro."""

ONZE = re.compile(r"(?<![0-9])[0-9]{11}(?![0-9])")
NOVE = re.compile(r"(?<![0-9])[0-9]{9}(?![0-9])")
PARTIDO = re.compile(r"(?<![0-9])[0-9]{9}[-. ][0-9]{2}(?![0-9])")
PONTUADO = re.compile(r"[0-9]{3}\.[0-9]{3}\.[0-9]{3}[-.][0-9]{2}")


RASTREADOS = (".py", ".md", ".toml", ".yml", ".cfg", ".txt")


def vistoriados() -> list[Path]:
    """O que o git rastreia — porque é exatamente isso que vai a público.

    O critério não é "arquivos do projeto": é o conjunto versionado. Um documento
    de trabalho local pode conter o que for necessário para pensar, e alguns
    contêm; o que não pode é ele atravessar para o repositório. Usar o índice do
    git faz a fronteira do teste coincidir com a fronteira da publicação.

    A fixture fica de fora porque tem guarda própria, mais estrita.
    """
    saida = subprocess.run(
        ["git", "ls-files"], cwd=RAIZ, capture_output=True, text=True, check=True
    ).stdout
    return sorted(
        RAIZ / linha
        for linha in saida.splitlines()
        if linha.endswith(RASTREADOS) and not linha.startswith("tests/fixtures/")
    )


def documentos_de(texto: str) -> set[str]:
    """Todo CPF válido no texto, em qualquer das quatro formas reconhecidas."""
    achados = set()
    for bruto in PARTIDO.findall(texto):
        if cpf_valido(bruto[:9] + bruto[10:]):
            achados.add(bruto[:9] + bruto[10:])
    for bruto in PONTUADO.findall(texto):
        somente = re.sub(r"[^0-9]", "", bruto)
        if cpf_valido(somente):
            achados.add(somente)
    for bruto in ONZE.findall(texto):
        if cpf_valido(bruto):
            achados.add(bruto)
    for bruto in NOVE.findall(texto):
        if cpf_valido(bruto.rjust(11, "0")):
            achados.add(bruto.rjust(11, "0"))
    return achados


def test_nenhum_cpf_valido_fora_dos_sinteticos_declarados() -> None:
    """A regra: CPF válido no repositório só existe se estiver declarado sintético."""
    intrusos = {
        f"{caminho.relative_to(RAIZ)}: {documento}"
        for caminho in vistoriados()
        for documento in documentos_de(caminho.read_text(encoding="utf-8"))
        if documento not in SINTETICOS
    }

    assert not intrusos, (
        "CPF válido não declarado como sintético:\n  "
        + "\n  ".join(sorted(intrusos))
        + "\nSe veio do arquivo da Receita, ele não entra: gere um sintético."
    )


def test_o_vistoriador_sabe_achar(tmp_path: Path) -> None:
    """Controle positivo. Um varredor que só sabe devolver conjunto vazio não
    varreu nada, e o teste acima passaria para sempre sem significar coisa
    alguma."""
    for forma in ("11144477735", "111.444.777-35", "111444777-35", "111444777 35"):
        assert documentos_de(f"NOME DE PESSOA {forma} LTDA"), forma


@pytest.mark.parametrize(
    "texto",
    [
        "TRANSPORTADORA 123456789 LTDA",
        "CONTRATO 123456789-99",
        "12345678901",
        "58254003000131",
        "sem digito nenhum",
    ],
)
def test_o_vistoriador_sabe_parar(texto: str) -> None:
    """O outro lado: número que não é CPF não pode ser acusado como se fosse.

    Sem isto, o vistoriador poderia estar apontando tudo, e a lista de sintéticos
    viraria uma lista de tudo que tem dígito.
    """
    assert not documentos_de(texto)


def test_os_sinteticos_sao_realmente_validos() -> None:
    """Se um deles deixasse de validar, o teste que ele existe para servir passaria
    a exercitar o caminho errado sem falhar."""
    assert all(cpf_valido(documento) for documento in SINTETICOS)
    assert len(SINTETICOS) == 4
