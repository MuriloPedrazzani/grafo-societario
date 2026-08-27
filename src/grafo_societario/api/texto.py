"""Número dentro de frase, escrito como quem lê espera.

## Número dentro de frase é prosa, e não protocolo

O `CLAUDE.md` divide o vocabulário: protocolo em inglês, domínio em português.
`f"{n:,}"` do Python escreve **3,154** — separador de milhar em inglês —, e isso
passa despercebido enquanto o número mora numa mensagem de exceção, que é lida
por quem opera o serviço.

Dentro de uma frase que a página mostra ao visitante, ele vira **outro número**:
`3,154` se lê como três e pouco em português. A tela do commit 40 mostrava o
título dizendo "3.154 vizinhos" e a explicação logo abaixo dizendo "3,154 nós" —
o mesmo número, escrito de dois jeitos, a dois centímetros de distância.

## O critério é quem lê, e não onde está

`api/deps.py` continua com `:,` nas mensagens que derrubam a partida, e está
certo: elas são para quem sobe o serviço, e o público delas é o mesmo do
`traceback` que vem junto. O que passa por aqui são as frases de `explicacao`,
que existem para aparecer na tela de quem consulta.

A separação não é de módulo, é de leitor — e por isso a guarda que impede a
regressão é sobre os módulos que montam `explicacao`, não sobre o projeto todo.
"""

from __future__ import annotations


def milhar(numero: int) -> str:
    """`3154` vira `3.154`.

    A troca é sobre o `f"{n:,}"` do Python, que usa a convenção inglesa. Não é
    `locale`: a formatação depende de o sistema ter a localidade instalada, e o
    contêiner da Fase 8 não tem — uma resposta que muda de formato conforme a
    máquina que a serve é pior que uma que sempre erra do mesmo jeito.
    """
    return f"{numero:,}".replace(",", ".")
