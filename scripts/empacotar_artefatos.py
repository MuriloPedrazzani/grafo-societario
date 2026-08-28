"""Monta o pacote da Release a partir dos artefatos construídos localmente.

Esta é a metade que **assembla**. A que confere é `conferir_release.py`, escrita
separada de propósito: se o mesmo código montasse e conferisse, a conferência não
pegaria defeito do montador — ela confirmaria apenas que ele foi consistente
consigo mesmo.

As duas se encontram em **duas constantes** — `ARTEFATOS_PUBLICAVEIS` e `TIPOS` —
e em nenhuma função. Nenhuma importa nada da outra.

## O tar é reprodutível

Mesmo conjunto de arquivos, mesmo tar, mesmo SHA-256. Isso exige apagar o que o
formato guarda de ambiente e não de conteúdo: `mtime`, dono, grupo e o carimbo de
tempo que o gzip escreve no próprio cabeçalho. Sem isso, empacotar duas vezes o
mesmo dado daria somas diferentes, e a soma deixaria de significar "este
conteúdo" para significar "esta execução".

## Uso

    python scripts/empacotar_artefatos.py --competencia 2026-06

Produz, em `dist/`:

    artefatos-<competencia>.tar.gz
    artefatos-<competencia>.tar.gz.sha256
"""

from __future__ import annotations

import argparse
import gzip
import io
import json
import sys
import tarfile
from pathlib import Path

RAIZ = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(RAIZ / "src"))

import numpy as np  # noqa: E402

from grafo_societario.graph.artefatos import (  # noqa: E402
    ARTEFATOS_PUBLICAVEIS,
    soma_do_arquivo,
)
from grafo_societario.graph.catalogo import TIPOS  # noqa: E402

MANIFESTO = "manifesto.json"
"""Único membro do tar que não é artefato.

Ele existe por uma razão só: sem ele não há como conferir que a **tag** da Release
descreve o que está **dentro** dela. Tag e conteúdo são escolhidos por gente em
momentos diferentes, e errar essa ligação produz o pior tipo de falha — uma imagem
que sobe normalmente servindo a competência errada, sem nada indicar que está
errada.
"""


def pessoas_fisicas_com_nome(origem: Path) -> int:
    """Quantas pessoas físicas têm nome gravado. No artefato publicável, zero.

    `atributos.npy` guarda o tipo nos dois bits baixos, e `nome_offsets.npy` dá a
    faixa de bytes de cada nó — faixa vazia quer dizer sem nome. O cruzamento dos
    dois responde, sem descomprimir nome nenhum, a única pergunta de privacidade
    que o artefato precisa responder sobre si mesmo.
    """
    atributos = np.load(origem / "atributos.npy", mmap_mode="r")
    offsets = np.load(origem / "nome_offsets.npy", mmap_mode="r")
    e_pessoa_fisica = (np.asarray(atributos) & 0b11) == TIPOS.index("pessoa_fisica")
    tem_nome = np.diff(np.asarray(offsets)) > 0
    return int((e_pessoa_fisica & tem_nome).sum())


def _informacao_normalizada(nome: str, tamanho: int) -> tarfile.TarInfo:
    """Membro sem nada que descreva a máquina que empacotou."""
    info = tarfile.TarInfo(nome)
    info.size = tamanho
    info.mtime = 0
    info.mode = 0o644
    info.uid = info.gid = 0
    info.uname = info.gname = ""
    return info


def montar(origem: Path, competencia: str, uf_alvo: str, destino: Path) -> Path:
    faltando = [nome for nome in ARTEFATOS_PUBLICAVEIS if not (origem / nome).exists()]
    if faltando:
        raise SystemExit(
            f"faltam {len(faltando)} artefatos em {origem}: {', '.join(faltando)}.\n"
            f"Construa antes de empacotar — o empacotador não constrói nada."
        )

    manifesto = {
        "competencia": competencia,
        "uf_alvo": uf_alvo,
        # Fato **medido**, não flag declarada. `EXPOR_PF` é configuração de quem
        # construiu, e configuração se erra; o que vai a público é o conteúdo.
        # Zero aqui é o que `EXPOR_PF=false` produz, e o conferidor recalcula o
        # mesmo número a partir do tar em vez de acreditar neste.
        "pessoas_fisicas_com_nome": pessoas_fisicas_com_nome(origem),
        "arquivos": {
            nome: {
                "sha256": soma_do_arquivo(origem / nome),
                "bytes": (origem / nome).stat().st_size,
            }
            for nome in ARTEFATOS_PUBLICAVEIS
        },
    }
    corpo = json.dumps(manifesto, ensure_ascii=False, indent=2, sort_keys=True).encode("utf-8")

    destino.mkdir(parents=True, exist_ok=True)
    pacote = destino / f"artefatos-{competencia}.tar.gz"

    # `mtime=0` no gzip e no tar: o carimbo de tempo mora nos dois, e deixar um
    # deles vivo já basta para a soma mudar entre execuções idênticas.
    with (
        pacote.open("wb") as bruto,
        gzip.GzipFile(filename="", mode="wb", fileobj=bruto, mtime=0) as comprimido,
        tarfile.open(fileobj=comprimido, mode="w") as tar,
    ):
        tar.addfile(_informacao_normalizada(MANIFESTO, len(corpo)), io.BytesIO(corpo))
        for nome in ARTEFATOS_PUBLICAVEIS:
            caminho = origem / nome
            with caminho.open("rb") as arquivo:
                tar.addfile(_informacao_normalizada(nome, caminho.stat().st_size), arquivo)

    soma = soma_do_arquivo(pacote)
    (destino / f"{pacote.name}.sha256").write_text(f"{soma}  {pacote.name}\n", encoding="utf-8")
    return pacote


def main() -> int:
    analisador = argparse.ArgumentParser(description=__doc__)
    analisador.add_argument("--competencia", required=True, help="no formato AAAA-MM")
    analisador.add_argument("--uf-alvo", default="SP")
    analisador.add_argument("--data-dir", type=Path, default=RAIZ / "data")
    analisador.add_argument("--destino", type=Path, default=RAIZ / "dist")
    opcoes = analisador.parse_args()

    origem = opcoes.data_dir / "grafo" / opcoes.competencia
    pacote = montar(origem, opcoes.competencia, opcoes.uf_alvo, opcoes.destino)

    tamanho = pacote.stat().st_size
    print(f"{pacote}  {tamanho / 1_000_000:.1f} MB")
    print(f"{pacote}.sha256")
    print()
    print("Publique como RASCUNHO — o workflow confere antes de tornar público:")
    print(
        f"  gh release create artefatos-{opcoes.competencia} --draft "
        f'--title "Artefatos {opcoes.competencia}" {pacote} {pacote}.sha256'
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
