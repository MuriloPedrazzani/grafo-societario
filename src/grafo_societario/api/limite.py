"""O limitador de taxa: contra varredura, não contra visitante.

## O propósito escolhe o número, e não o contrário

**Não é proteção contra pico de visitante.** Se o link circular e cinquenta
pessoas clicarem ao mesmo tempo, travá-las mata exatamente aquilo que a fase
existe para mostrar. Por isso o balde é **por cliente**: cinquenta pessoas são
cinquenta baldes, e nenhuma atrapalha a outra.

É contra **varredura** — um cliente sozinho consultando CNPJ por CNPJ até
reconstruir o artefato. Com o propósito escrito, o número sai da conta:

| | |
|---|---:|
| CNPJs no recorte | 19.770.618 |
| limite por cliente | 60 por minuto |
| tempo de varredura ininterrupta | **229 dias** |

Sessenta por minuto é um por segundo **sustentado**, que nenhuma pessoa
navegando alcança — e a página da Fase 7 dispara poucas requisições por clique.
A varredura, no mesmo limite, leva sete meses e meio sem parar.

## A varredura já era desnecessária, e é isso que o 429 diz

O artefato é **publicado em GitHub Release**. Quem quer os 19,7 milhões baixa 416
MB e tem tudo, inclusive o que a API não devolve. Varrer a rota é o caminho mais
lento para chegar ao mesmo lugar, então o limite não precisa ser draconiano — e a
mensagem do `429` aponta o caminho sancionado, porque limite que só diz "não"
empurra para a varredura mais devagar em vez de torná-la desnecessária.

O que o limite protege de fato é a **instância**: uma consulta de vizinhança num
hub custa até 320 ms, e um cliente a 60 por minuto já ocupa a terça parte de um
núcleo. Cem clientes assim derrubariam o free tier para todo mundo.

## `X-Forwarded-For` é escrito por quem chama

Atrás de um proxy o IP do cliente chega num cabeçalho, e **confiar nele sem
qualificar deixa qualquer um furar o limite trocando o valor a cada
requisição**. O cabeçalho é acrescentado da esquerda para a direita, então a
entrada mais à direita é a que o proxy mais próximo escreveu; tudo à esquerda
dela veio de fora e é forjável.

Daí `PROXIES_CONFIAVEIS`: quantos saltos **você controla**. Com `0`, que é o
padrão, o cabeçalho é ignorado e vale o IP da conexão — o único que não se forja.

**O padrão está seguro e não está certo para o deploy.** Atrás do proxy do free
tier todo mundo chega com o IP dele, e um balde único faria o limite valer para a
soma dos visitantes. O valor certo depende de quantos saltos existem entre o
cliente e o processo, e isso só se sabe no commit 44 — até lá o `/health` declara
qual está em uso, para o erro ser visível em vez de silencioso.

## O contador é estado mutável compartilhado, e aqui é inevitável

Diferente do cache do catálogo, que foi recusado porque havia alternativa, aqui
não há: contar exige escrever. Então o contador tem lock, e o lock foi medido:

| threads no mesmo balde | mediana | p95 |
|---:|---:|---:|
| 1 | 0,40 µs | 0,50 µs |
| 8 | 0,40 µs | 0,50 µs |
| 16 | 0,40 µs | 0,50 µs |

**A contenção não aparece**, e o conjunto sustenta 1,6 milhão de registros por
segundo. Contra os 8,01 ms de mediana de uma consulta, o limitador é **0,005% da
resposta** — o remédio não virou problema.

O contraste com o cache recusado no commit 33 é exato e vale entender: lá o lock
teria serializado `zlib`, que **solta a GIL** e por isso escalava de verdade —
custo real. Aqui a seção crítica é um `get` e um `set` de dicionário, que a GIL
já serializa de qualquer jeito. **O lock só custa onde havia paralelismo a
perder.**

**A janela é fixa e global.** Todos os baldes viram no mesmo instante, o que
permite esvaziar o dicionário inteiro de uma vez: sem isso, um cliente trocando
de IP a cada requisição faria o limitador crescer sem teto e virar o vazamento
que ele deveria evitar. O preço é o pico de fronteira — até o dobro do limite
numa janela curta, se as requisições caírem em volta da virada. É aceitável para
um limite cujo alvo mede em meses.

## O contador não vale entre réplicas

É memória de processo. Duas instâncias são dois contadores, e o limite efetivo
dobra. No free tier há uma instância só, então funciona — mas isso é **fato do
deploy, não propriedade do desenho**, e escalar horizontalmente exige trocar o
armazenamento por algo compartilhado.
"""

from __future__ import annotations

import logging
import threading
import time
from dataclasses import dataclass, field
from typing import Final

from fastapi import HTTPException, Request

logger = logging.getLogger(__name__)

JANELA: Final = 60
"""Segundos da janela. Um minuto, que é a unidade em que o limite é declarado."""

SEM_CLIENTE: Final = "desconhecido"
"""Quando não há IP de conexão. Todos caem no mesmo balde, que é o lado seguro."""


def cliente_de(request: Request, proxies_confiaveis: int) -> str:
    """Quem é o cliente, confiando **apenas** nos saltos declarados.

    `X-Forwarded-For` cresce da esquerda para a direita: cada proxy acrescenta o
    endereço de quem falou com ele. A entrada mais à direita é, portanto, a que o
    proxy mais próximo escreveu, e é a única que ele garante.

    Controlando `n` saltos, o cliente é a `n`-ésima entrada contada da direita. O
    que estiver à esquerda dela foi enviado por quem chamou e não vale nada — é
    exatamente o valor que alguém trocaria a cada requisição para furar o limite.
    """
    if proxies_confiaveis > 0:
        encaminhados = [
            parte.strip()
            for parte in request.headers.get("x-forwarded-for", "").split(",")
            if parte.strip()
        ]
        if len(encaminhados) >= proxies_confiaveis:
            return encaminhados[-proxies_confiaveis]
    return request.client.host if request.client else SEM_CLIENTE


@dataclass
class Limitador:
    """Contador de requisições por cliente, em janela fixa de um minuto."""

    por_minuto: int
    proxies_confiaveis: int
    _trava: threading.Lock = field(default_factory=threading.Lock, repr=False)
    _janela: int = -1
    _contagem: dict[str, int] = field(default_factory=dict, repr=False)

    def registrar(self, cliente: str, agora: float) -> tuple[bool, int]:
        """Conta a requisição e devolve `(permitida, segundos até a janela virar)`.

        A contagem sobe mesmo quando a requisição é recusada: quem insiste
        continua no mesmo balde até a virada, em vez de ganhar folga por bater na
        porta.
        """
        janela = int(agora // JANELA)
        with self._trava:
            if janela != self._janela:
                self._janela = janela
                self._contagem.clear()
            contagem = self._contagem.get(cliente, 0) + 1
            self._contagem[cliente] = contagem
        return contagem <= self.por_minuto, JANELA - int(agora % JANELA)

    def contagem_de(self, cliente: str) -> int:
        with self._trava:
            return self._contagem.get(cliente, 0)

    def rastreados(self) -> int:
        """Quantos clientes há no dicionário. Existe para o teste provar que a
        virada da janela o esvazia, e que ele não cresce sem teto."""
        with self._trava:
            return len(self._contagem)


def limitar(request: Request) -> None:
    """A dependência que as rotas de consulta declaram. `429` com `Retry-After`.

    `/health` fica de fora de propósito: a plataforma o consulta para decidir se
    manda tráfego, e limitá-lo faria o limitador derrubar a própria instância.
    """
    limitador: Limitador | None = getattr(request.app.state, "limitador", None)
    if limitador is None:
        return
    cliente = cliente_de(request, limitador.proxies_confiaveis)
    permitida, faltam = limitador.registrar(cliente, time.monotonic())
    if permitida:
        return
    logger.warning(
        "limite de taxa atingido",
        extra={"rota": request.url.path, "limite_por_minuto": limitador.por_minuto},
    )
    raise HTTPException(
        status_code=429,
        detail=(
            f"Limite de {limitador.por_minuto} requisições por minuto atingido. Ele existe "
            "contra varredura, e não contra uso: se você precisa do recorte inteiro, o artefato "
            "é publicado em GitHub Release e traz mais do que esta rota devolve — baixá-lo é "
            "mais rápido que consultar 19,7 milhões de CNPJs."
        ),
        headers={"Retry-After": str(faltam)},
    )
