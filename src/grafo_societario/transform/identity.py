"""Normalização de nome de pessoa física, e o limite dela.

A identidade de pessoa física neste projeto é o par nome mais CPF mascarado. A
máscara deixa seis dígitos visíveis, e no recorte de SP são **584.902 máscaras
distintas para 5.635.007 identidades** — 9,6 pessoas por máscara, e **49,4% das
máscaras já são compartilhadas por mais de um nome**. O nome faz quase todo o
trabalho de distinguir uma pessoa da outra.

Isso é o que torna cada degrau de normalização caro: ele apaga parte do que
distingue, e transfere o trabalho para a coincidência. Normalizar demais funde
pessoas diferentes; normalizar de menos separa a mesma pessoa. Nenhum dos dois
erros aparece como erro — os dois aparecem como grafo.

## Os três tipos de degrau, e por que a normalização para onde para

Esta é a regra que decide o que entra aqui, e existe para que a pergunta "posso
acrescentar mais um?" tenha resposta antes de alguém tentar.

**Tipo 1 — variação de codificação.** Maiúscula, acento, espaço. Não carregam
informação de identidade nenhuma: `JOSÉ` e `JOSE` são o mesmo nome escrito por
teclados diferentes. São seguros **por construção**, e não por medição — nenhum
dado poderia torná-los perigosos. Entram.

**Tipo 2 — ruído gramatical.** Partícula: `DA`, `DAS`, `DE`, `DI`, `DO`, `DOS`,
`E`. Não distinguem pessoas; distinguem estilos de preenchimento de formulário.
Não são seguros por construção, então foram **medidos**: sobre 5.635.007
identidades, a remoção funde **sete**, e as sete são a mesma pessoa digitada de
dois jeitos, com a mesma máscara de CPF — `MARIA APARECIDA SILVA` e `MARIA
APARECIDA DA SILVA`, `RAFAEL OLIVEIRA SILVA` e `RAFAEL DE OLIVEIRA SILVA`. Sem
o degrau, essas sete pessoas ficariam como catorze nós. Entra.

**Tipo 3 — informação.** Inicial do meio, sobrenome intermediário, apelido. A
inicial existe **para** distinguir: é o que separa `JOSE C SILVA` de `JOSE A
SILVA`. Remover mede zero fusão hoje, mas o mecanismo funde **por construção**
assim que dois portadores da mesma máscara diferirem só pela inicial — e com
metade das máscaras já compartilhada, isso vai acontecer. Seguro que pode causar
o sinistro não é seguro. **Não entra, e este parágrafo é a razão.**

Se você veio aqui para acrescentar um degrau, o teste é esse: ele apaga
codificação, gramática, ou informação? Só os dois primeiros passam. "Remover
sobrenome do meio" é tipo 3, mesmo parecendo tão inofensivo quanto remover `DA`.

## Nome ausente não é nome vazio

`normalizar_nome` devolve `None` — nunca `""` — quando não sobra nome. São 370
vínculos de pessoa física no recorte de SP sem nome nenhum, e para eles a
identidade seria a máscara sozinha, que funde todos os que a compartilham. Esse
é exatamente o pior caso que os números acima descrevem.

Devolver `None` obriga quem gera o identificador a decidir o que fazer com o
caso, em vez de hashear string vazia e fundir por acidente. A decisão em si é da
geração de identidade, não daqui.
"""

from __future__ import annotations

import unicodedata
from typing import Final

import duckdb

PARTICULAS: Final = frozenset({"DA", "DAS", "DE", "DI", "DO", "DOS", "E"})
"""Partículas removidas como **token inteiro**, nunca como prefixo.

A distinção não é detalhe: `DANIEL` começa com `DA` e `EDUARDO` começa com `E`.
Remover por prefixo transformaria os dois em outra pessoa, e o erro passaria por
normalização bem-sucedida.
"""


def normalizar_nome(nome: str | None) -> str | None:
    """Reduz um nome à forma que a identidade compara.

    Aplica, nesta ordem: remoção de acento, maiúscula, colapso de espaço e
    remoção de partícula. Devolve `None` quando não sobra nome — entrada nula,
    vazia, só espaço, ou composta apenas de partículas.
    """
    if nome is None:
        return None

    sem_acento = unicodedata.normalize("NFKD", nome).encode("ascii", "ignore").decode("ascii")
    tokens = [token for token in sem_acento.upper().split() if token not in PARTICULAS]
    return " ".join(tokens) or None


_LISTA_DE_PARTICULAS: Final = ", ".join(f"'{particula}'" for particula in sorted(PARTICULAS))

MACRO_DE_NORMALIZACAO: Final = f"""
CREATE OR REPLACE TEMP MACRO normalizar_nome(nome) AS (
  nullif(
    array_to_string(
      list_filter(
        string_split_regex(upper(strip_accents(coalesce(nome, ''))), '\\s+'),
        token -> token <> '' AND token NOT IN ({_LISTA_DE_PARTICULAS})
      ), ' '),
    '')
);
"""
"""A mesma regra no motor, para as 8,4 milhões de linhas da camada silver.

Existe em SQL porque um `UDF` em Python seria chamado uma vez por vínculo. Existe
também em Python porque a regra precisa ser legível e testável fora do motor. Duas
implementações do mesmo algoritmo divergem no commit em que ninguém olha, e é por
isso que há um teste comparando as duas — ele é o que as mantém sendo uma só.
"""


def instalar_normalizacao(conexao: duckdb.DuckDBPyConnection) -> None:
    """Registra `normalizar_nome` na conexão do DuckDB. Idempotente."""
    conexao.execute(MACRO_DE_NORMALIZACAO)
