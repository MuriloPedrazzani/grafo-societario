"""O CNPJ na borda da API: entra conferido, sai completo.

## Por que quatorze dígitos, e não os oito do básico

O grafo é indexado por `cnpj_basico`, então aceitar oito dígitos seria mais
simples de implementar e mais cômodo de usar. **É justamente por isso que não se
aceita.** O básico não tem dígito verificador: `11222334` é um `cnpj_basico`
plausível tanto quanto `11222333`, e um erro de digitação viraria consulta a
outra empresa — resposta com aparência correta, sobre a companhia errada, sem
nada falhando.

Os dois dígitos finais são a única defesa que existe contra isso, e ela custa uma
conta de doze multiplicações. Exigi-los transforma o erro de digitação de
**resposta errada** em **422**, que é a troca que este projeto faz em toda borda.

## E por que a resposta devolve os quatorze

Pela simetria: o que sai de uma resposta tem de entrar na requisição seguinte.
Quem segue um caminho societário copia o CNPJ de um salto para consultar o
próximo, e devolver o básico obrigaria essa pessoa a inventar ordem e
verificador.

O custo é zero em artefato. **A matriz é sempre ordem `0001`** — é a definição de
matriz na fonte da Receita, e o recorte do projeto é por UF da matriz —, e o
verificador se calcula. Nenhum byte a mais em disco para devolver o que o
artefato não guarda.

## O `cnpj_basico` é inteiro aqui e texto na fonte

No bronze e no silver ele é texto, porque código é sempre texto e zero à esquerda
se perde na conversão. No artefato do grafo ele é `int32`, porque a busca binária
de 20 MB precisa de array numérico. Este módulo é a fronteira entre as duas
representações, e a formatação recompõe o zero com largura fixa — `f"{n:08d}"`, e
nunca concatenação do número.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Final

DIGITOS: Final = re.compile(r"^\d{14}$")
NAO_DIGITO: Final = re.compile(r"[.\-/\s]")
"""Os separadores da máscara oficial, e mais nada. Uma limpeza que apagasse
qualquer não-dígito aceitaria `1a1b2c2...` como CNPJ."""

PESOS_PRIMEIRO: Final = (5, 4, 3, 2, 9, 8, 7, 6, 5, 4, 3, 2)
PESOS_SEGUNDO: Final = (6, 5, 4, 3, 2, 9, 8, 7, 6, 5, 4, 3, 2)

ORDEM_DA_MATRIZ: Final = "0001"
"""A ordem do estabelecimento matriz, por definição da Receita Federal.

O recorte do projeto é por UF da **matriz**, e os nós do grafo são empresas
identificadas pelo `cnpj_basico` — então o estabelecimento que a resposta nomeia
é sempre este."""


class CnpjInvalidoError(ValueError):
    """O texto recebido não é um CNPJ de quatorze dígitos com verificador válido."""


@dataclass(frozen=True)
class Cnpj:
    """Um CNPJ já conferido, partido nas três peças que a resposta usa."""

    cnpj_basico: int
    ordem: str
    verificador: str

    @property
    def completo(self) -> str:
        base = f"{self.cnpj_basico:08d}"
        return f"{base[:2]}.{base[2:5]}.{base[5:8]}/{self.ordem}-{self.verificador}"


def _digito(digitos: str, pesos: tuple[int, ...]) -> str:
    soma = sum(int(digito) * peso for digito, peso in zip(digitos, pesos, strict=True))
    resto = soma % 11
    return str(0 if resto < 2 else 11 - resto)


def digitos_verificadores(base: str) -> str:
    """Os dois dígitos verificadores dos doze primeiros de um CNPJ.

    `base` são `cnpj_basico` e ordem concatenados, doze dígitos. O segundo dígito
    depende do primeiro, e por isso o cálculo é encadeado e não paralelo.
    """
    if len(base) != 12 or not base.isdigit():
        raise CnpjInvalidoError(
            f"O verificador se calcula sobre os doze primeiros dígitos; veio {base!r}."
        )
    primeiro = _digito(base, PESOS_PRIMEIRO)
    return primeiro + _digito(base + primeiro, PESOS_SEGUNDO)


def analisar(texto: str) -> Cnpj:
    """Confere o CNPJ e o parte, ou recusa dizendo o que corrigir.

    Aceita com máscara e sem — quem cola de uma resposta traz a máscara, quem
    integra por programa manda os dígitos.
    """
    limpo = NAO_DIGITO.sub("", texto.strip())
    if not DIGITOS.match(limpo):
        raise CnpjInvalidoError(
            f"{texto!r} não é um CNPJ. São quatorze dígitos, com ou sem máscara — por exemplo "
            "11.222.333/0001-81 ou 11222333000181. O cnpj_basico de oito dígitos não é aceito: "
            "ele não tem verificador, e um erro de digitação viraria consulta a outra empresa."
        )
    esperado = digitos_verificadores(limpo[:12])
    if limpo[12:] != esperado:
        raise CnpjInvalidoError(
            f"O verificador de {texto!r} não confere: seria {esperado}. É o dígito que separa "
            "erro de digitação de consulta silenciosa à empresa errada."
        )
    return Cnpj(cnpj_basico=int(limpo[:8]), ordem=limpo[8:12], verificador=esperado)


def formatar(cnpj_basico: int, ordem: str = ORDEM_DA_MATRIZ) -> str:
    """O CNPJ completo da matriz, com o verificador calculado.

    O artefato guarda oito dígitos; esta função devolve os quatorze que a resposta
    mostra e que a requisição seguinte aceita de volta.
    """
    verificador = digitos_verificadores(f"{cnpj_basico:08d}{ordem}")
    return Cnpj(cnpj_basico=cnpj_basico, ordem=ordem, verificador=verificador).completo
