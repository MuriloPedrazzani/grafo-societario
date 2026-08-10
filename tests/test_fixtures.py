"""Invariantes das fixtures.

Fixture é artefato publicado e permanente. Estes testes existem para que ninguém
— inclusive eu, daqui a seis meses — adicione uma amostra com e-mail, telefone ou
CPF válido sem que a suíte reclame na hora.

Também travam as características de formato que o parser do bronze precisa
exercitar: se uma delas sumir de uma fixture, o teste que a cobria passaria a
testar nada, em silêncio.
"""

from __future__ import annotations

import csv
import re
from pathlib import Path

import pytest

FIXTURES = Path(__file__).parent / "fixtures"
LIMITE_DE_BYTES = 128 * 1024

CAMPOS = {
    "Empresas0.csv": 7,
    "Estabelecimentos0.csv": 30,
    "Socios0.csv": 11,
    "Cnaes.csv": 2,
    "Motivos.csv": 2,
    "Municipios.csv": 2,
    "Naturezas.csv": 2,
    "Paises.csv": 2,
    "Qualificacoes.csv": 2,
}

CONTATO = list(range(21, 28))  # ddd_1 .. correio_eletronico, base zero
DIGITOS_SEGUIDOS = re.compile(r"\d{8,}")
CPF_MASCARADO = re.compile(r"^\*\*\*\d{6}\*\*$")


def registros(nome: str) -> list[list[str]]:
    with (FIXTURES / nome).open(encoding="latin-1", newline="") as arquivo:
        return list(csv.reader(arquivo, delimiter=";", quotechar='"'))


def cru(nome: str) -> bytes:
    return (FIXTURES / nome).read_bytes()


def cpf_valido(numero: str) -> bool:
    if len(numero) != 11 or not numero.isdigit() or len(set(numero)) == 1:
        return False
    for tamanho in (9, 10):
        soma = sum(int(numero[i]) * (tamanho + 1 - i) for i in range(tamanho))
        if (soma * 10 % 11) % 10 != int(numero[tamanho]):
            return False
    return True


# ------------------------------------------------------------------ formato


@pytest.mark.parametrize("nome", sorted(CAMPOS))
def test_fixture_e_pequena(nome: str) -> None:
    assert (FIXTURES / nome).stat().st_size <= LIMITE_DE_BYTES


@pytest.mark.parametrize("nome", sorted(CAMPOS))
def test_decodifica_em_latin1(nome: str) -> None:
    cru(nome).decode("latin-1")


@pytest.mark.parametrize("nome", sorted(CAMPOS))
def test_terminador_e_lf_sem_cr(nome: str) -> None:
    assert b"\r\n" not in cru(nome)


@pytest.mark.parametrize(("nome", "quantidade"), sorted(CAMPOS.items()))
def test_todo_registro_tem_a_quantidade_certa_de_campos(nome: str, quantidade: int) -> None:
    assert all(len(registro) == quantidade for registro in registros(nome))


@pytest.mark.parametrize("nome", sorted(CAMPOS))
def test_todos_os_campos_vem_entre_aspas(nome: str) -> None:
    """Inclusive numérico e vazio — é assim que a Receita grava."""
    primeira = cru(nome).split(b"\n", maxsplit=1)[0]
    assert primeira.startswith(b'"')
    assert primeira.endswith(b'"')


def test_campo_vazio_e_gravado_como_aspas_vazias() -> None:
    assert b';"";' in cru("Estabelecimentos0.csv")


# --------------------------------------------------------------- privacidade


def test_estabelecimentos_nao_tem_nenhum_contato() -> None:
    for registro in registros("Estabelecimentos0.csv"):
        assert all(registro[indice] == "" for indice in CONTATO)


def test_nenhuma_fixture_contem_arroba_de_email() -> None:
    for nome in CAMPOS:
        assert b"@" not in cru(nome), f"{nome} parece conter endereço de e-mail"


def test_socios_nao_tem_cpf_mascarado_de_pessoa_real() -> None:
    """Todo documento de PF tem de vir do gerador sintético, não da fonte."""
    for registro in registros("Socios0.csv"):
        if registro[1] == "2":
            assert CPF_MASCARADO.match(registro[3]), registro[3]
        elif registro[1] == "3":
            assert registro[3] == ""


def test_nenhum_numero_de_onze_digitos_e_cpf_valido() -> None:
    """A redação usa dígitos reprovados no verificador, para não colidir com CPF real."""
    for nome in ("Empresas0.csv", "Socios0.csv", "Estabelecimentos0.csv"):
        for registro in registros(nome):
            for campo in registro:
                for achado in DIGITOS_SEGUIDOS.findall(campo):
                    for inicio in range(len(achado) - 10):
                        assert not cpf_valido(achado[inicio : inicio + 11]), f"{nome}: {achado}"


def test_empresas_teve_a_razao_social_redigida_na_maioria() -> None:
    """A proporção precisa espelhar o arquivo real, onde 78% é empresário individual."""
    linhas = registros("Empresas0.csv")
    individuais = [registro for registro in linhas if registro[2] == "2135"]
    assert len(individuais) / len(linhas) > 0.5


# ------------------------------------------------ armadilhas para o parser


def test_ha_separador_dentro_de_campo_citado() -> None:
    for nome in ("Estabelecimentos0.csv", "Socios0.csv"):
        assert any(";" in campo for registro in registros(nome) for campo in registro), nome


def test_ha_quebra_de_linha_dentro_de_campo_citado() -> None:
    for nome in ("Estabelecimentos0.csv", "Socios0.csv"):
        assert any("\n" in campo for registro in registros(nome) for campo in registro), nome


def test_linhas_fisicas_excedem_registros_por_causa_da_quebra() -> None:
    """É a diferença que denuncia parser que conta linha em vez de registro."""
    fisicas = cru("Estabelecimentos0.csv").count(b"\n")
    assert fisicas > len(registros("Estabelecimentos0.csv"))


def test_ha_o_byte_0x8f_que_cp1252_rejeita() -> None:
    for nome in ("Estabelecimentos0.csv", "Socios0.csv"):
        assert b"\x8f" in cru(nome), nome
        with pytest.raises(UnicodeDecodeError):
            cru(nome).decode("cp1252")


def test_nao_ha_byte_atribuido_exclusivamente_ao_cp1252() -> None:
    """Se aparecesse, a conclusão sobre a codificação teria de ser revista."""
    atribuidos = {
        0x82,
        0x83,
        0x84,
        0x85,
        0x86,
        0x87,
        0x88,
        0x89,
        0x8A,
        0x8B,
        0x8C,
        0x8E,
        0x91,
        0x92,
        0x93,
        0x94,
        0x95,
        0x96,
        0x97,
        0x98,
        0x99,
        0x9A,
        0x9B,
        0x9C,
        0x9E,
        0x9F,
    }
    for nome in CAMPOS:
        assert not (set(cru(nome)) & atribuidos), nome


def test_ha_codigo_com_zero_a_esquerda() -> None:
    porte = {registro[5] for registro in registros("Empresas0.csv")}
    assert {"00", "01", "05"} & porte
    situacao = {registro[5] for registro in registros("Estabelecimentos0.csv")}
    assert any(codigo.startswith("0") and len(codigo) > 1 for codigo in situacao)


@pytest.mark.parametrize(
    "nome",
    [
        "Empresas0.csv",
        "Estabelecimentos0.csv",
        "Socios0.csv",
        "Cnaes.csv",
        "Naturezas.csv",
        "Paises.csv",
        "Qualificacoes.csv",
    ],
)
def test_ha_acento_preservado(nome: str) -> None:
    """Vale também para o conteúdo sintético: substituir por ASCII puro apagaria
    justamente a característica que exercita a decodificação latin-1."""
    conteudo = cru(nome).decode("latin-1")
    assert any(0xC0 <= ord(caractere) <= 0xFF for caractere in conteudo), nome


def test_cnae_secundaria_traz_lista_separada_por_virgula() -> None:
    secundarias = [registro[12] for registro in registros("Estabelecimentos0.csv")]
    assert any("," in valor for valor in secundarias)
