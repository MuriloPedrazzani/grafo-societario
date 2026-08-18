"""O CNPJ na borda: quatorze dígitos, verificador conferido, completo na volta.

O teste que dá sentido aos outros é o do verificador. Aceitar o `cnpj_basico` de
oito dígitos seria conveniente e faria erro de digitação virar consulta a **outra
empresa**, em silêncio e com resposta de aparência correta — que é o modo de
falha que este projeto trata como o pior.
"""

from __future__ import annotations

import pytest

from grafo_societario.api.cnpj import (
    Cnpj,
    CnpjInvalidoError,
    analisar,
    digitos_verificadores,
    formatar,
)

# Bases sintéticas: as três da fixture do grafo, mais o exemplo canônico da
# documentação. Nenhuma é escolhida por pertencer a empresa nenhuma.
CANONICO = "11.222.333/0001-81"


def test_aceita_com_mascara_e_sem() -> None:
    assert analisar(CANONICO) == Cnpj(cnpj_basico=11222333, ordem="0001", verificador="81")
    assert analisar("11222333000181") == analisar(CANONICO)
    assert analisar("  11222333000181  ") == analisar(CANONICO)


def test_preserva_o_zero_a_esquerda_do_basico() -> None:
    """`cnpj_basico` é texto na fonte e vira inteiro aqui; o zero volta na saída.

    Um `int` perde o zero à esquerda, e é por isso que a formatação recompõe com
    largura fixa em vez de concatenar o número.
    """
    cnpj = analisar("00.360.305/0001-04")

    assert cnpj.cnpj_basico == 360305
    assert cnpj.completo == "00.360.305/0001-04"


@pytest.mark.parametrize(
    "texto",
    [
        "11222333",  # o básico sozinho: é exatamente o que não pode passar
        "11.222.333/0001",  # sem verificador
        "11222333000180",  # verificador errado por um dígito
        "11222333000191",  # os dois dígitos errados
        "1122233300018",  # treze dígitos
        "112223330001812",  # quinze
        "11.222.333/0001-8X",  # letra onde vai dígito
        "",
        "abcdefghijklmn",
    ],
)
def test_recusa_o_que_nao_e_cnpj_de_quatorze_digitos(texto: str) -> None:
    with pytest.raises(CnpjInvalidoError):
        analisar(texto)


def test_a_mensagem_de_recusa_diz_o_que_fazer() -> None:
    """Erro de borda que não diz o que corrigir vira ticket de suporte."""
    with pytest.raises(CnpjInvalidoError, match="quatorze"):
        analisar("11222333")


@pytest.mark.parametrize(
    ("base", "verificador"),
    [
        ("112223330001", "81"),
        ("111111110001", "91"),
        ("222222220001", "91"),
        ("333333330001", "91"),
        ("003603050001", "04"),
    ],
)
def test_calcula_o_verificador_dos_doze_primeiros(base: str, verificador: str) -> None:
    assert digitos_verificadores(base) == verificador


def test_o_verificador_e_o_do_algoritmo_e_nao_o_do_texto() -> None:
    """Controle negativo do cálculo: se ele devolvesse o que já estava lá, a
    validação aceitaria qualquer coisa e nenhum teste acima falharia."""
    assert digitos_verificadores("112223330001") != "00"
    assert digitos_verificadores("111111110001") != digitos_verificadores("112223330001")


def test_formata_a_matriz_a_partir_do_basico() -> None:
    """Matriz é sempre ordem `0001`, e o verificador sai do cálculo.

    É o que permite a resposta devolver CNPJ completo sem que o artefato carregue
    byte nenhum além dos oito dígitos do básico.
    """
    assert formatar(11222333) == CANONICO
    assert formatar(11111111) == "11.111.111/0001-91"
    assert formatar(360305) == "00.360.305/0001-04"


def test_o_completo_volta_a_ser_analisavel() -> None:
    """Ida e volta: o que a resposta devolve, a requisição seguinte aceita.

    Sem isso, quem copia um CNPJ de uma resposta para consultar o próximo salto
    recebe 422 no valor que o próprio serviço acabou de emitir.
    """
    for cnpj_basico in (11222333, 11111111, 360305, 99999999):
        assert analisar(formatar(cnpj_basico)).cnpj_basico == cnpj_basico
