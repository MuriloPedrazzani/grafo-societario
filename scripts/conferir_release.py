"""Confere um pacote de Release contra o que este commit declara publicável.

Esta é a metade que **lê o resultado**. A que monta é `empacotar_artefatos.py`, e
as duas são implementações separadas de propósito: conferência escrita pelo mesmo
código que assembla não pega defeito do montador, só confirma que ele foi
consistente consigo mesmo.

O que as duas compartilham são **duas constantes** — `ARTEFATOS_PUBLICAVEIS` e
`TIPOS` — e nenhuma função. Há teste lendo a AST das duas e exigindo isso.

## O que só o CI faz, e por isso é o que está aqui

Refazer a soma que o empacotador acabou de fazer, na mesma máquina, prova pouco.
O valor de conferir num ambiente limpo é outro:

1. **O conjunto é exatamente o que ESTE commit declara.** Nem sobra nem falta.
   Isso amarra o artefato publicado a uma versão do código — ninguém consegue
   verificar essa ligação depois, olhando só a Release.

2. **As somas batem**, arquivo por arquivo e do pacote inteiro. Pega upload
   truncado e corrupção de transporte, que a máquina que empacotou não vê.

3. **A competência da tag bate com a de dentro.** Tag e conteúdo são escolhidos
   por gente em momentos diferentes. Errar a ligação publica uma Release rotulada
   errado, o Dockerfile puxa o artefato errado achando que acertou, e a imagem
   sobe normalmente servindo a competência errada. É falha silenciosa, e é
   barata de pegar exatamente aqui.

4. **Nenhuma pessoa física tem nome no pacote**, recalculado a partir dos
   próprios arrays de dentro dele. `EXPOR_PF` é configuração de quem construiu, e
   configuração se erra; conferir o número que o manifesto declara seria acreditar
   em quem empacotou. Publicar nome de pessoa física é o único erro desta lista
   que não tem volta — ativo de Release se apaga, não se desbaixa.

## Uso

    python scripts/conferir_release.py --tag artefatos-2026-06 --pacote dist/x.tar.gz
"""

from __future__ import annotations

import argparse
import hashlib
import io
import json
import re
import sys
import tarfile
from pathlib import Path
from typing import Any, cast

RAIZ = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(RAIZ / "src"))

import numpy as np  # noqa: E402

from grafo_societario.graph.artefatos import (  # noqa: E402
    ARTEFATOS_PUBLICAVEIS,
    TIPOS,
)

TAG = re.compile(r"^artefatos-(?P<competencia>\d{4}-\d{2})$")
BLOCO = 1024 * 1024


class ReleaseInvalidaError(RuntimeError):
    """O pacote não descreve o que a tag promete."""


def _soma(fluxo: object) -> str:
    digestor = hashlib.sha256()
    while pedaco := fluxo.read(BLOCO):  # type: ignore[attr-defined]
        digestor.update(pedaco)
    return digestor.hexdigest()


def _array_do_tar(tar: tarfile.TarFile, nome: str) -> np.ndarray[Any, np.dtype[Any]]:
    """`np.load` é tipado como devolvendo `Any` porque também abre `.npz`. Aqui só
    entram `.npy` de array simples, e o `cast` diz isso ao verificador em vez de
    silenciá-lo."""
    fluxo = tar.extractfile(nome)
    if fluxo is None:
        raise ReleaseInvalidaError(f"{nome} não é um arquivo comum dentro do pacote")
    return cast("np.ndarray[Any, np.dtype[Any]]", np.load(io.BytesIO(fluxo.read())))


def _pessoas_fisicas_com_nome(tar: tarfile.TarFile) -> int:
    """Cruza tipo e faixa de nome, lendo os dois arrays de dentro do pacote.

    O tipo mora nos dois bits baixos de `atributos.npy`; a faixa de bytes do nome,
    em `nome_offsets.npy`. Faixa vazia é nó sem nome — que é o que toda pessoa
    física tem no artefato publicável.
    """
    atributos = _array_do_tar(tar, "atributos.npy")
    offsets = _array_do_tar(tar, "nome_offsets.npy")
    if offsets.size != atributos.size + 1:
        raise ReleaseInvalidaError(
            f"nome_offsets tem {offsets.size} entradas para {atributos.size} nós; "
            f"o esperado é um a mais que o número de nós"
        )
    e_pessoa_fisica = (atributos & 0b11) == TIPOS.index("pessoa_fisica")
    return int((e_pessoa_fisica & (np.diff(offsets) > 0)).sum())


def _competencia_da_tag(tag: str) -> str:
    achado = TAG.match(tag)
    if achado is None:
        raise ReleaseInvalidaError(
            f"tag {tag!r} fora do formato `artefatos-AAAA-MM`, que é o que o Dockerfile monta"
        )
    return achado.group("competencia")


def conferir(pacote: Path, tag: str, soma_publicada: str) -> dict[str, object]:
    esperada = _competencia_da_tag(tag)

    with pacote.open("rb") as arquivo:
        soma_do_pacote = _soma(arquivo)
    if soma_do_pacote != soma_publicada:
        raise ReleaseInvalidaError(
            f"a soma do pacote é {soma_do_pacote}, e o .sha256 publicado diz {soma_publicada}. "
            f"Upload truncado ou arquivo trocado."
        )

    with tarfile.open(pacote, "r:gz") as tar:
        membros = tar.getnames()
        if len(membros) != len(set(membros)):
            raise ReleaseInvalidaError("o pacote tem membros repetidos")

        declarados = {*ARTEFATOS_PUBLICAVEIS, "manifesto.json"}
        presentes = set(membros)
        if presentes != declarados:
            sobra = sorted(presentes - declarados)
            falta = sorted(declarados - presentes)
            raise ReleaseInvalidaError(
                f"o pacote não bate com ARTEFATOS_PUBLICAVEIS deste commit.\n"
                f"  sobrando: {sobra or 'nada'}\n"
                f"  faltando: {falta or 'nada'}"
            )

        extraido = tar.extractfile("manifesto.json")
        if extraido is None:
            raise ReleaseInvalidaError("manifesto.json não é um arquivo comum")
        manifesto = json.loads(extraido.read().decode("utf-8"))

        dentro = manifesto.get("competencia")
        if dentro != esperada:
            raise ReleaseInvalidaError(
                f"a tag diz competência {esperada!r} e o manifesto diz {dentro!r}. "
                f"A Release está rotulada errado, e o Dockerfile puxaria o artefato "
                f"errado achando que acertou."
            )

        # A pergunta de privacidade, respondida a partir do tar e não do
        # manifesto. Publicar artefato com nome de pessoa física é o erro que
        # não tem volta: ativo de Release se apaga, não se desbaixa.
        #
        # Recalcular aqui, e não conferir o número que o montador anotou, é o que
        # torna esta checagem independente — manifesto é afirmação de quem
        # empacotou, e quem empacotou pode ter empacotado com `EXPOR_PF` ligada.
        com_nome = _pessoas_fisicas_com_nome(tar)
        if com_nome != 0:
            raise ReleaseInvalidaError(
                f"{com_nome:,} pessoas físicas têm nome gravado no artefato. "
                f"O publicável é construído com EXPOR_PF desligada, e este pacote "
                f"não foi.".replace(",", ".")
            )
        anotado = manifesto.get("pessoas_fisicas_com_nome")
        if anotado != 0:
            raise ReleaseInvalidaError(
                f"o manifesto declara {anotado!r} pessoas físicas com nome; "
                f"o esperado é 0, e a ausência do campo também reprova"
            )

        anotadas = manifesto.get("arquivos", {})
        divergentes = []
        for nome in ARTEFATOS_PUBLICAVEIS:
            fluxo = tar.extractfile(nome)
            if fluxo is None:
                raise ReleaseInvalidaError(f"{nome} não é um arquivo comum dentro do pacote")
            calculada = _soma(fluxo)
            if calculada != anotadas.get(nome, {}).get("sha256"):
                divergentes.append(nome)
        if divergentes:
            raise ReleaseInvalidaError(
                f"soma divergente em {len(divergentes)} arquivo(s): {', '.join(divergentes)}"
            )

    return {
        "competencia": esperada,
        "uf_alvo": manifesto.get("uf_alvo"),
        "arquivos": len(ARTEFATOS_PUBLICAVEIS),
        "sha256": soma_do_pacote,
    }


def main() -> int:
    analisador = argparse.ArgumentParser(description=__doc__)
    analisador.add_argument("--tag", required=True)
    analisador.add_argument("--pacote", type=Path, required=True)
    analisador.add_argument(
        "--soma",
        type=Path,
        default=None,
        help="arquivo .sha256; o padrão é <pacote>.sha256",
    )
    opcoes = analisador.parse_args()

    caminho_da_soma = opcoes.soma or Path(f"{opcoes.pacote}.sha256")
    soma_publicada = caminho_da_soma.read_text(encoding="utf-8").split()[0]

    try:
        resumo = conferir(opcoes.pacote, opcoes.tag, soma_publicada)
    except ReleaseInvalidaError as erro:
        print(f"RECUSADO: {erro}", file=sys.stderr)
        return 1

    print(
        f"conferido: competência {resumo['competencia']}, UF {resumo['uf_alvo']}, "
        f"{resumo['arquivos']} artefatos, sha256 {resumo['sha256']}"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
