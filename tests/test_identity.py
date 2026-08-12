"""Normalização de nome: o que ela apaga, o que ela preserva, e onde ela para.

Os testes estão agrupados pelos três tipos de degrau que o módulo distingue.
Os do tipo 3 são os mais importantes: eles não testam o que a normalização faz,
testam o que ela **se recusa** a fazer. Sem eles, acrescentar "remove inicial do
meio" passaria na suíte inteira.
"""

from __future__ import annotations

import duckdb
import pytest

from grafo_societario.transform.identity import (
    PARTICULAS,
    instalar_normalizacao,
    normalizar_nome,
)

# ------------------------------- tipo 1: variação de codificação, sempre segura


@pytest.mark.parametrize(
    ("cru", "esperado"),
    [
        ("jose silva", "JOSE SILVA"),
        ("Jose Silva", "JOSE SILVA"),
        ("JOSÉ SILVA", "JOSE SILVA"),
        ("JOSE MÜLLER", "JOSE MULLER"),
        ("MARIA DA CONCEIÇÃO", "MARIA CONCEICAO"),
        ("ANTÔNIO JOÃO", "ANTONIO JOAO"),
        ("  JOSE   SILVA  ", "JOSE SILVA"),
        ("JOSE\tSILVA", "JOSE SILVA"),
        ("JOSE\nSILVA", "JOSE SILVA"),
    ],
)
def test_variacao_de_codificacao_e_apagada(cru: str, esperado: str) -> None:
    assert normalizar_nome(cru) == esperado


def test_acento_e_maiuscula_nunca_fundem_pessoas_diferentes() -> None:
    """Tipo 1 é seguro por construção: `JOSÉ` e `JOSE` são o mesmo nome escrito
    em teclados diferentes, e nenhum dado poderia tornar isso perigoso."""
    assert normalizar_nome("JOSÉ SILVA") == normalizar_nome("JOSE SILVA")
    assert normalizar_nome("MARIA SOUSA") != normalizar_nome("MARIA SOUZA")


# ----------------------------------- tipo 2: ruído gramatical, medido e aceito


@pytest.mark.parametrize(
    ("cru", "esperado"),
    [
        ("JOSE DA SILVA", "JOSE SILVA"),
        ("MARIA APARECIDA DOS SANTOS", "MARIA APARECIDA SANTOS"),
        ("ANTONIO FERREIRA DA COSTA", "ANTONIO FERREIRA COSTA"),
        ("JOAO BATISTA DE OLIVEIRA", "JOAO BATISTA OLIVEIRA"),
        ("RAFAEL DOS SANTOS SILVA", "RAFAEL SANTOS SILVA"),
        ("LUIZ DI CAVALCANTI", "LUIZ CAVALCANTI"),
        ("PEDRO E PAULO SOUZA", "PEDRO PAULO SOUZA"),
        ("MARIA DAS DORES", "MARIA DORES"),
        ("DA SILVA", "SILVA"),
    ],
)
def test_particula_e_removida(cru: str, esperado: str) -> None:
    assert normalizar_nome(cru) == esperado


@pytest.mark.parametrize(
    ("cru", "esperado"),
    [
        ("DANIEL SANTOS", "DANIEL SANTOS"),
        ("EDUARDO LIMA", "EDUARDO LIMA"),
        ("DOMINGOS DIAS", "DOMINGOS DIAS"),
        ("DALVA DOS REIS", "DALVA REIS"),
        ("ELIANE DE SOUZA", "ELIANE SOUZA"),
    ],
)
def test_particula_so_sai_como_token_inteiro(cru: str, esperado: str) -> None:
    """`DANIEL` começa com `DA` e `EDUARDO` começa com `E`.

    Remover por prefixo transformaria os dois em outra pessoa, e o erro passaria
    por normalização bem-sucedida — sem exceção, sem nulo, sem nada acusar.
    """
    assert normalizar_nome(cru) == esperado


def test_os_sete_casos_reais_que_o_degrau_funde() -> None:
    """As sete fusões medidas sobre 5.635.007 identidades do recorte de SP.

    Todas com a mesma máscara de CPF, todas a mesma pessoa digitada de dois
    jeitos. É a evidência direta que sustenta aceitar o degrau — mais forte que o
    argumento probabilístico, e é ela que este teste preserva.
    """
    pares = [
        ("ANTONIO FERREIRA DA COSTA", "ANTONIO FERREIRA COSTA"),
        ("APARECIDO DA SILVA", "APARECIDO SILVA"),
        ("JOAO BATISTA DE OLIVEIRA", "JOAO BATISTA OLIVEIRA"),
        ("JOSE APARECIDO SANTOS", "JOSE APARECIDO DOS SANTOS"),
        ("MARIA APARECIDA SILVA", "MARIA APARECIDA DA SILVA"),
        ("RAFAEL OLIVEIRA SILVA", "RAFAEL DE OLIVEIRA SILVA"),
        ("RAFAEL DOS SANTOS SILVA", "RAFAEL SANTOS SILVA"),
    ]
    for um, outro in pares:
        assert normalizar_nome(um) == normalizar_nome(outro), f"{um} != {outro}"


# ---------------------------- tipo 3: informação, recusada — os testes-guarda


def test_inicial_do_meio_e_preservada() -> None:
    """A recusa do degrau 5, travada.

    A inicial existe **para** distinguir. Remover mede zero fusão hoje, mas funde
    por construção assim que dois portadores da mesma máscara diferirem só por
    ela — e 49,4% das máscaras do recorte já são compartilhadas.

    Se este teste passar a falhar, alguém acrescentou a remoção de inicial
    achando que seguia o mesmo princípio das partículas. Não segue: partícula é
    gramática, inicial é informação.
    """
    assert normalizar_nome("JOSE C SILVA") == "JOSE C SILVA"
    assert normalizar_nome("JOSE C SILVA") != normalizar_nome("JOSE A SILVA")
    assert normalizar_nome("JOSE C SILVA") != normalizar_nome("JOSE SILVA")


def test_sobrenome_do_meio_e_preservado() -> None:
    """Mesma categoria da inicial, e o próximo candidato a ser removido por engano."""
    assert normalizar_nome("MARIA APARECIDA SILVA") != normalizar_nome("MARIA SILVA")


def test_particula_nao_cresce_sozinha() -> None:
    """A lista é fechada e pequena de propósito: cada acréscimo é um degrau novo,
    e degrau novo exige medição nova."""
    assert set(PARTICULAS) == {"DA", "DAS", "DE", "DI", "DO", "DOS", "E"}


# ------------------------------------------------- nome ausente não é nome vazio


@pytest.mark.parametrize("cru", [None, "", "   ", "\t\n", "DE", "DA DOS", "E"])
def test_sem_nome_devolve_nulo_e_nunca_string_vazia(cru: str | None) -> None:
    """String vazia hasheada com a máscara fundiria todos os sem-nome que a
    compartilham. Devolver nulo obriga quem gera identidade a decidir."""
    assert normalizar_nome(cru) is None


def test_nome_que_sobra_de_uma_letra_permanece() -> None:
    """Nome de uma letra é pouco, mas é informação — e não é ausência."""
    assert normalizar_nome("J SILVA") == "J SILVA"


# ------------------------------------------- as duas implementações são uma só


CASOS_DE_EQUIVALENCIA = [
    None,
    "",
    "   ",
    "jose da silva",
    "JOSÉ DA SILVA",
    "  MARIA   DAS  DORES ",
    "ANTÔNIO JOÃO DA CONCEIÇÃO",
    "DANIEL DOS SANTOS",
    "EDUARDO DE LIMA",
    "JOSE C SILVA",
    "LUIZ DI CAVALCANTI",
    "DE",
    "MÜLLER",
    "J SILVA",
    "PEDRO E PAULO",
]


@pytest.mark.parametrize("cru", CASOS_DE_EQUIVALENCIA)
def test_macro_sql_concorda_com_a_funcao_python(cru: str | None) -> None:
    """Duas implementações do mesmo algoritmo divergem no commit em que ninguém
    olha. A de SQL existe para as 8,4 milhões de linhas; a de Python, para ser
    legível e testável. Este teste é o que as mantém sendo uma regra só."""
    with duckdb.connect() as conexao:
        instalar_normalizacao(conexao)
        obtido = conexao.execute("SELECT normalizar_nome(?)", [cru]).fetchone()

    assert obtido is not None
    assert obtido[0] == normalizar_nome(cru), cru


def test_a_equivalencia_tem_caso_que_exercita_cada_degrau() -> None:
    """Controle positivo do teste acima: comparar duas implementações em casos que
    nenhuma transforma provaria apenas que ambas sabem devolver a entrada."""
    transformados = [
        cru for cru in CASOS_DE_EQUIVALENCIA if cru and normalizar_nome(cru) != cru.strip()
    ]
    assert len(transformados) >= 8
