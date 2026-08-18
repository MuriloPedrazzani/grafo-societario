"""A página é servida pela própria API, e não por um host separado.

## Uma origem, e não duas

A alternativa seria publicar a página em GitHub Pages e deixá-la chamar a API por
CORS. Foi recusada por três motivos, em ordem crescente de peso:

1. **Sem CORS.** Mesma origem não precisa de cabeçalho de permissão, e cabeçalho
   de permissão é configuração que erra em silêncio.
2. **Uma configuração só.** Duas hospedagens são dois lugares onde a URL da outra
   pode ficar velha.
3. **Um despertar, e não dois.** Num plano gratuito que hiberna, cada origem que
   o navegador toca é uma instância a acordar. A demonstração já paga essa espera
   uma vez; pagá-la duas seria o dobro do pior primeiro segundo.

**A consequência é honesta e fica escrita: se a API cair, a demonstração cai
junto.** A demonstração *é* a API — ela não tem dado próprio nem cópia local, e é
exatamente isso que faz o que ela mostra ser o que o serviço responde.

## Estas rotas ficam fora do limitador

O visitante gastaria o balde carregando a própria página. O limite existe contra
varredura da API, e HTML e CSS não são a API — nenhum dos dois toca o grafo.

## O `/health` preenche o primeiro segundo

A página dispara a consulta do exemplo ao abrir, e essa consulta pode esperar a
instância acordar. O `/health` é barato, não é limitado e responde com competência
e tamanho do grafo — é o que a pessoa lê enquanto o resto chega.
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Final

from fastapi import APIRouter, FastAPI
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles

logger = logging.getLogger(__name__)

RAIZ: Final = Path(__file__).resolve().parent.parent / "web"
"""Onde moram `index.html` e `static/`.

Dentro do pacote, e não na raiz do repositório: assim eles viajam no wheel e na
imagem da Fase 8 sem regra de cópia própria."""

PAGINA: Final = RAIZ / "index.html"
ESTATICOS: Final = RAIZ / "static"

roteador = APIRouter(include_in_schema=False)
"""Fora do OpenAPI de propósito: `/docs` descreve a API, e a página não é API."""


@roteador.get("/")
def pagina() -> FileResponse:
    """A página de consulta. Mesma origem da API que ela consome."""
    return FileResponse(PAGINA, media_type="text/html")


class PaginaAusenteError(RuntimeError):
    """Os arquivos da página não vieram junto do pacote."""


def montar(app: FastAPI) -> None:
    """Instala a página e os estáticos na aplicação.

    Levanta se os arquivos não estiverem no lugar. Uma aplicação que sobe
    servindo `404` na raiz é pior que uma que não sobe, pelo mesmo motivo do
    `/health` verde com grafo ausente: o defeito só aparece pelo visitante, e
    depois de o balanceador já ter mandado tráfego.
    """
    faltando = [caminho for caminho in (PAGINA, ESTATICOS) if not caminho.exists()]
    if faltando:
        raise PaginaAusenteError(
            f"Faltam arquivos da página em {RAIZ}: {', '.join(c.name for c in faltando)}. "
            "Eles viajam dentro do pacote, então a falta indica empacotamento incompleto."
        )
    app.include_router(roteador)
    app.mount("/static", StaticFiles(directory=ESTATICOS), name="static")
