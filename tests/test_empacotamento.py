"""O que o empacotamento entrega, que é pergunta diferente do que o código importa.

`test_api.py` já exige, num subprocesso, que importar a aplicação não traga
`duckdb`, `scipy` nem `pyarrow` para `sys.modules`. Essa guarda passou verde
durante toda a Fase 6 e a Fase 7 — e a imagem de deploy embarcava **58 MB de
DuckDB** o tempo todo, porque a dependência continuava declarada em
`dependencies`.

A fronteira estava imposta no nível de **import** e não no de **dependência**.
Nada acusava, porque nada perguntava. É a mesma forma da vistoria de dado pessoal
que não abria `.js`: a regra existia, a imposição tinha um buraco.

Este módulo pergunta a outra metade, e do jeito mais direto que existe: instala o
pacote **sem extra nenhum**, num ambiente virgem, e exige que a API suba ali.

Isso prova as duas direções de uma vez, que é o que uma inspeção de metadados não
faria: que o conjunto base é **suficiente** — se faltar dependência, o import
quebra — e que ele é **mínimo** — se sobrar, o `find_spec` acha.
"""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

import pytest

RAIZ = Path(__file__).resolve().parents[1]

FORA_DO_SERVING = ("duckdb", "scipy", "pyarrow", "typer", "httpx", "tenacity")
"""O que não pode chegar junto com o conjunto base.

`duckdb` e `scipy` são construção; `typer`, `httpx` e `tenacity` são a CLI, que a
imagem de deploy não executa — ela sobe `uvicorn` direto. `pyarrow` nunca foi
dependência e está aqui como controle: se ele aparecesse, seria por alguém ter
trazido um leitor de Parquet para o caminho de resposta.
"""


def _python_do(ambiente: Path) -> Path:
    return ambiente / ("Scripts/python.exe" if os.name == "nt" else "bin/python")


@pytest.fixture(scope="module")
def instalacao_minima(tmp_path_factory: pytest.TempPathFactory) -> Path:
    """Ambiente virgem com `pip install .` e nada mais.

    Escopo de módulo porque criar o ambiente e instalar leva ~90 s, e as duas
    perguntas abaixo se respondem sobre a mesma instalação.
    """
    ambiente = tmp_path_factory.mktemp("minima") / "venv"
    subprocess.run([sys.executable, "-m", "venv", str(ambiente)], check=True)
    subprocess.run(
        [
            str(_python_do(ambiente)),
            "-m",
            "pip",
            "install",
            "--quiet",
            "--disable-pip-version-check",
            str(RAIZ),
        ],
        check=True,
    )
    return ambiente


@pytest.mark.lento
def test_a_api_sobe_com_o_conjunto_base(instalacao_minima: Path) -> None:
    """Suficiência: sem extra nenhum, a aplicação tem de importar.

    Uma inspeção de metadados diria que `duckdb` saiu, e não diria se o que
    sobrou basta. Mover dependência demais quebra o deploy e não quebra teste
    nenhum que rode no ambiente de desenvolvimento, onde tudo está instalado.
    """
    codigo = (
        "import grafo_societario.api.main as m; "
        "print(hasattr(m, 'criar_aplicacao') and hasattr(m, 'app'))"
    )

    saida = subprocess.run(
        [str(_python_do(instalacao_minima)), "-c", codigo],
        capture_output=True,
        text=True,
        check=True,
    )

    assert saida.stdout.strip() == "True", (
        f"a aplicação não sobe com o conjunto base: {saida.stdout!r} {saida.stderr!r}"
    )


@pytest.mark.lento
def test_o_conjunto_base_nao_arrasta_construcao_nem_cli(instalacao_minima: Path) -> None:
    """Minimalidade, e a pergunta é `find_spec`, não `sys.modules`.

    `sys.modules` responde "foi importado", que é o que a guarda de `test_api.py`
    já cobre. Aqui a pergunta é "está instalado" — foi exatamente essa que
    ninguém fazia enquanto 58 MB de DuckDB viajavam para dentro da imagem.
    """
    codigo = (
        "import importlib.util as u; "
        f"print([n for n in {FORA_DO_SERVING!r} if u.find_spec(n) is not None])"
    )

    saida = subprocess.run(
        [str(_python_do(instalacao_minima)), "-c", codigo],
        capture_output=True,
        text=True,
        check=True,
    )

    assert saida.stdout.strip() == "[]", (
        f"o conjunto base arrastou {saida.stdout.strip()} — o que não responde consulta "
        f"não entra na imagem de deploy"
    )


def test_o_extra_de_desenvolvimento_traz_os_dois_grupos() -> None:
    """`dev` tem de incluir `build` e `cli`, senão a suíte deixa de rodar.

    Esta é barata e vale a pena separada: ela reprova a edição que move
    dependência para um extra e esquece de somá-la ao `dev` — que é a forma mais
    provável de este commit dar errado, e que quebraria o CI inteiro em vez de
    uma linha.
    """
    conteudo = (RAIZ / "pyproject.toml").read_text(encoding="utf-8")

    assert "grafo-societario[build,cli]" in conteudo, (
        "`dev` precisa incluir os dois extras; sem isso a suíte perde DuckDB ou Typer"
    )
