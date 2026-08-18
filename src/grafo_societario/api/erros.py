"""Nenhum `500` genérico, e nenhuma exceção interna com cara de resposta.

## O que o cliente recebe, e o que você consegue achar depois

Toda exceção não tratada vira uma resposta estruturada com um **identificador de
correlação**. O cliente recebe algo acionável — um código para citar — e o log
recebe o mesmo código junto do `run_id` que o formatador já emite em toda linha.
Sem ele, "deu erro na sua API" é um relato que não se investiga.

## As exceções internas são defeito, não resposta

`NoForaDaFaixaError`, `ErroDeCsr` e as do catálogo descrevem artefato
inconsistente ou índice fora da faixa. Nenhuma delas é uma condição que o cliente
possa corrigir, e nenhuma delas deve chegar até ele com o nome da classe ou o
rastro de pilha: isso é `500` com aparência de resposta, e ainda entrega o mapa
das entranhas.

Elas caem aqui, viram `500` com o identificador, e o rastro fica no log — que é
onde ele serve para alguma coisa.

## O que **não** passa por aqui

`404`, `422` e `429` são respostas legítimas, decididas pelas rotas, e continuam
com o corpo que elas montaram. Este módulo cobre o que ninguém previu.
"""

from __future__ import annotations

import logging
import uuid
from typing import Final

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse

logger = logging.getLogger(__name__)

TAMANHO_DO_ID: Final = 12
"""Metade de um uuid4. Curto o bastante para alguém ditar por telefone, longo o
bastante para achar a linha no log de um dia."""

RECADO: Final = (
    "Erro interno ao responder esta consulta. O identificador abaixo está registrado no log do "
    "serviço — cite-o ao relatar. A consulta em si não tem nada de errado: repetir depois é "
    "razoável."
)


def registrar_tratadores(app: FastAPI) -> None:
    """Instala o tratador que impede o `500` genérico."""

    @app.exception_handler(Exception)
    async def qualquer_excecao(request: Request, excecao: Exception) -> JSONResponse:
        erro_id = uuid.uuid4().hex[:TAMANHO_DO_ID]
        logger.exception(
            "erro não tratado na resposta",
            extra={
                "erro_id": erro_id,
                "rota": request.url.path,
                "excecao": type(excecao).__name__,
            },
        )
        return JSONResponse(status_code=500, content={"detail": RECADO, "erro_id": erro_id})
