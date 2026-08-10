"""Garante que o pacote é importável e que sua versão é única.

`pyproject.toml` declara a versão como `dynamic`, lida de `__init__.py` pelo
build backend. O teste fecha esse laço: compara o que o módulo expõe com o que
foi de fato gravado nos metadados da distribuição instalada.
"""

from importlib.metadata import version

import grafo_societario


def test_pacote_expoe_versao() -> None:
    assert isinstance(grafo_societario.__version__, str)
    assert grafo_societario.__version__


def test_versao_do_modulo_bate_com_a_da_distribuicao() -> None:
    assert grafo_societario.__version__ == version("grafo-societario")
