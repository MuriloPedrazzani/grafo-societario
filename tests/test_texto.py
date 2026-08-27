"""Número dentro de frase é prosa, e prosa deste projeto é em português.

O defeito que originou este módulo apareceu na tela do commit 40: o título dizia
"3.154 vizinhos" e a explicação logo abaixo dizia "3,154 nós". O mesmo número,
escrito de dois jeitos, a dois centímetros um do outro — e o segundo se lê como
três e pouco em português.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from grafo_societario.api.texto import milhar

MODULOS_QUE_FALAM_COM_GENTE = ("caminho.py", "empresa.py", "vizinhanca.py")
RAIZ = Path(__file__).resolve().parents[1] / "src" / "grafo_societario" / "api"


@pytest.mark.parametrize(
    ("numero", "escrito"),
    [
        (0, "0"),
        (7, "7"),
        (999, "999"),
        (1000, "1.000"),
        (3154, "3.154"),
        (19_770_618, "19.770.618"),
    ],
)
def test_escreve_o_milhar_como_em_portugues(numero: int, escrito: str) -> None:
    assert milhar(numero) == escrito


def test_nao_depende_de_localidade_instalada() -> None:
    """`locale` mudaria o formato conforme a máquina que serve a resposta.

    O contêiner da Fase 8 não traz localidade nenhuma, e uma API que responde
    diferente conforme onde roda é pior que uma que erra sempre igual.
    """
    from grafo_societario.api import texto

    fonte = Path(texto.__file__).read_text(encoding="utf-8")

    assert "import locale" not in fonte, "a formatação não pode depender do ambiente"


@pytest.mark.parametrize("modulo", MODULOS_QUE_FALAM_COM_GENTE)
def test_quem_monta_explicacao_nao_usa_o_separador_ingles(modulo: str) -> None:
    """A guarda é por **leitor**, e não por módulo do projeto.

    `api/deps.py` continua com `:,` nas mensagens que derrubam a partida, e está
    certo: elas são lidas por quem sobe o serviço, junto do `traceback`. Estes
    três montam o campo `explicacao`, que existe para aparecer na tela de quem
    consulta — e ali o separador inglês troca o número.
    """
    fonte = (RAIZ / modulo).read_text(encoding="utf-8")

    assert ":,}" not in fonte, (
        f"{modulo} formata número com o separador inglês numa frase que vai para a tela. "
        "Use `texto.milhar`."
    )
