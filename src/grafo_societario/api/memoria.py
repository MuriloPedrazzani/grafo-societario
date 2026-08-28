"""A residente do processo que responde, lida sem dependência nova.

## Por que não `psutil`

Ele mediria isto em uma linha, e está no extra `dev` — fora da imagem de deploy,
por decisão do commit que separou os grupos. Trazê-lo de volta ao conjunto base
para um campo de relatório desfaria essa separação e somaria peso à imagem para
responder consulta. `/proc/self/status` é biblioteca padrão e não custa nada.

## O que ele mede, e por que isso importa para o commit 44

`VmRSS` é a residente do processo: memória anônima **mais** as páginas de arquivo
mapeadas que foram tocadas. O acervo é lido com `mmap`, então cada consulta que
alcança uma região nova do grafo deixa páginas residentes.

O risco contra os 512 MB do free tier **não é uma consulta cara** — é a residente
subindo com a **cobertura** do grafo ao longo do tempo. Medido: o custo de uma
travessia ao componente gigante é ~92 MB, e uma requisição rejeitada com `422`,
que não toca o grafo, deixa a residente praticamente no mesmo lugar. As duas
medições descrevem residência de base, não custo de consulta.

Por isso este campo existe: ele é o instrumento da soneira que responde se a
curva **estabiliza ou sobe**. Página limpa de arquivo é recuperável sob pressão,
então a expectativa é que estabilize — e é exatamente a expectativa que a
medição serve para substituir.

## Nulo, nunca zero

Sem `/proc`, o campo vem `None` e o JSON traz `null`. **Nunca zero.** Zero é um
número, e número errado se propaga como se fosse medição — foi o `0,00 GiB` do
`GetProcessMemoryInfo` não conferido que ensinou isso. Esta API roda em Windows
no desenvolvimento e em Linux no deploy, então a ausência é o caso normal
metade do tempo, não uma borda.

## É relatório, não portão

O `/health` **não reprova** por causa deste campo. Ele responde `ok` com
`residente_bytes: null` do mesmo jeito que com um número. Transformá-lo em
condição de saúde faria uma medição de diagnóstico derrubar a instância — e o
que ele mede não é falha, é uso.
"""

from __future__ import annotations

from pathlib import Path

STATUS = Path("/proc/self/status")
"""Onde o Linux publica a residente. Não existe em Windows nem em macOS."""


def residente_em_bytes() -> int | None:
    """`VmRSS` do processo em bytes, ou `None` onde não há `/proc`.

    Devolve `None` também quando o arquivo existe e não traz `VmRSS`, ou traz
    algo que não é número: leitura que falhou não vira zero.
    """
    try:
        conteudo = STATUS.read_text(encoding="utf-8")
    except OSError:
        return None

    for linha in conteudo.splitlines():
        if not linha.startswith("VmRSS:"):
            continue
        partes = linha.split()
        if len(partes) < 2:
            return None
        try:
            return int(partes[1]) * 1024
        except ValueError:
            return None
    return None
