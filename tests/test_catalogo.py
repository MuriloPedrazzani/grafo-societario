"""O catálogo de nós: ida e volta, arrays paralelos, e o tipo do alvo da busca.

Dois testes aqui não conferem valor, e sim mecanismo. O primeiro é a ida e volta
sobre todo nome, porque deslocamento errado por um byte devolve o nome de outra
empresa sem mudar contagem nenhuma. O segundo é o tipo do alvo da busca binária,
que custou mil vezes o tempo de uma consulta e não mudou uma única resposta.
"""

from __future__ import annotations

import subprocess
import sys
import zlib
from pathlib import Path
from typing import Any

import numpy as np
import pytest

from grafo_societario.config import Config
from grafo_societario.graph import catalogo as modulo
from grafo_societario.graph.build import gerar_nos
from grafo_societario.graph.catalogo import (
    ArtefatoAusenteError,
    ArtefatosIncompativeisError,
    NoForaDaFaixaError,
    abrir_catalogo,
)
from grafo_societario.graph.metadados import (
    BLOCO,
    NomeDivergenteError,
    NosAusentesError,
    TaxaDivergenteError,
    _conferir_ida_e_volta,
    _conferir_taxa,
    _tabela_de_taxas,
    serializar_metadados,
)
from grafo_societario.transform.identity import gerar_identidades
from grafo_societario.transform.silver import (
    aplicar_recorte_por_uf,
    tipar_empresas,
    tipar_socios,
)
from test_silver import (
    NATUREZAS_PADRAO,
    PAISES_PADRAO,
    QUALIFICACOES_PADRAO,
    _gravar_dominio,
    empresa,
    estabelecimento,
    gravar_empresas,
    gravar_estabelecimentos,
    gravar_socios,
    socio,
)


@pytest.fixture
def construido(tmp_path: Path) -> Config:
    """Duas empresas ligadas por uma pessoa física, e uma empresa isolada."""
    config = Config(competencia="2026-06", data_dir=tmp_path, uf_alvo="SP")
    gravar_estabelecimentos(
        config, [estabelecimento(cnpj) for cnpj in ("11111111", "22222222", "33333333")]
    )
    aplicar_recorte_por_uf(config)
    gravar_empresas(
        config,
        [
            empresa("11111111", razao_social="ALFA COMERCIO LTDA"),
            empresa("22222222", razao_social="BRAVO SERVICOS SA"),
            empresa("33333333", razao_social="CHARLIE SOZINHA ME"),
        ],
    )
    _gravar_dominio(config, "Naturezas", NATUREZAS_PADRAO)
    _gravar_dominio(config, "Qualificacoes", QUALIFICACOES_PADRAO)
    _gravar_dominio(config, "Paises", PAISES_PADRAO)
    tipar_empresas(config)
    gravar_socios(
        config,
        [
            socio("11111111", nome="FULANO DE TAL", documento="***123458**"),
            socio("22222222", nome="FULANO DE TAL", documento="***123458**"),
        ],
    )
    tipar_socios(config)
    gerar_identidades(config)
    gerar_nos(config)
    serializar_metadados(config)
    return config


BASES_POR_REGIAO = tuple(f"9000000{digito}" for digito in range(10))
"""Uma empresa por dígito de região fiscal."""

SEM_NOME = "90000010"
DE_FORA = "90000011"


@pytest.fixture
def todas_as_regioes(tmp_path: Path) -> Config:
    """Um nó de pessoa física por dígito de região, mais os dois que não têm taxa.

    A máscara do CPF é `***DDDDDD**`, e a **nona posição** é o dígito da região —
    o último dos seis visíveis. Variar só ele produz os dez regimes.
    """
    config = Config(competencia="2026-06", data_dir=tmp_path, uf_alvo="SP")
    bases = [*BASES_POR_REGIAO, SEM_NOME, DE_FORA]
    gravar_estabelecimentos(config, [estabelecimento(cnpj) for cnpj in bases])
    aplicar_recorte_por_uf(config)
    gravar_empresas(config, [empresa(cnpj, razao_social=f"EMPRESA {cnpj}") for cnpj in bases])
    _gravar_dominio(config, "Naturezas", NATUREZAS_PADRAO)
    _gravar_dominio(config, "Qualificacoes", QUALIFICACOES_PADRAO)
    _gravar_dominio(config, "Paises", PAISES_PADRAO)
    tipar_empresas(config)
    gravar_socios(
        config,
        [
            *(
                socio(base, nome=f"PESSOA NUMERO {digito}", documento=f"***12345{digito}**")
                for digito, base in enumerate(BASES_POR_REGIAO)
            ),
            # Sócio sem nome: tem região e é `nao_fundivel`, então não tem taxa.
            socio(SEM_NOME, nome="", documento="***123456**"),
            # Estrangeiro: não tem documento nenhum, logo nem região nem taxa.
            socio(DE_FORA, tipo="3", nome="ECHO FOXTROT", documento="", pais="249"),
        ],
    )
    tipar_socios(config)
    gerar_identidades(config)
    gerar_nos(config)
    serializar_metadados(config)
    return config


def _gravar_tabela(config: Config, tabela: np.ndarray[Any, np.dtype[Any]]) -> None:
    alvo = config.data_dir / "grafo" / "2026-06" / "taxa_por_regiao.npy"
    with alvo.open("wb") as arquivo:
        np.save(arquivo, tabela, allow_pickle=False)


# ------------------------------------------------- taxa de colisão por região


def test_a_taxa_e_indexada_pelo_digito_e_nao_pela_posicao(todas_as_regioes: Config) -> None:
    """A correspondência dígito → posição, conferida nos dez.

    O risco aqui é menor que o de array paralelo por nó — são dez posições —, mas
    não é zero: quem "otimizar" para guardar só os dígitos presentes faz a região
    0 virar a região 1 no primeiro mês em que um dígito não aparecer, e nada
    falha. A tabela gravada tem um valor que **denuncia o dígito de onde veio**.
    """
    _gravar_tabela(todas_as_regioes, np.array([d / 1000 for d in range(10)], dtype=np.float64))
    cat = abrir_catalogo(todas_as_regioes)

    vistos = {
        cat.regiao_de(no): cat.taxa_de_colisao_de(no)
        for no in range(cat.nos)
        if cat.confianca_de(no) == "estimada"
    }

    assert len(vistos) == 10, "a fixture precisa cobrir os dez dígitos"
    for digito, taxa in vistos.items():
        assert digito is not None
        assert taxa == int(digito) / 1000, f"o dígito {digito} leu a posição errada"


def test_pessoa_fisica_sem_nome_tem_regiao_e_nao_tem_taxa(todas_as_regioes: Config) -> None:
    """`nao_fundivel` não é dado faltando: a grandeza não se aplica.

    Taxa de colisão mede fusão por máscara de CPF, e sócio sem nome não é fundido
    por máscara nenhuma — cada registro dele vira um nó próprio. No dado real são
    **370 nós que têm região e não têm taxa**, e derivar a taxa só da região daria
    um número a todos eles.
    """
    cat = abrir_catalogo(todas_as_regioes)

    nao_fundiveis = [no for no in range(cat.nos) if cat.confianca_de(no) == "nao_fundivel"]

    assert nao_fundiveis, "a fixture precisa de um sócio sem nome"
    for no in nao_fundiveis:
        assert cat.regiao_de(no) is not None, "ele tem região"
        assert cat.taxa_de_colisao_de(no) is None, "e mesmo assim não tem taxa"


@pytest.mark.parametrize("confianca", ["exata", "fraca"])
def test_quem_nao_e_fundido_por_mascara_nao_tem_taxa(
    todas_as_regioes: Config, confianca: str
) -> None:
    """Pessoa jurídica é identificada exatamente pelo CNPJ; estrangeiro não tem
    documento nenhum. Nos dois a taxa não se aplica, e nula é a resposta certa."""
    cat = abrir_catalogo(todas_as_regioes)

    nos = [no for no in range(cat.nos) if cat.confianca_de(no) == confianca]

    assert nos, f"a fixture precisa de um nó com confiança {confianca}"
    assert all(cat.taxa_de_colisao_de(no) is None for no in nos)


def test_digito_sem_taxa_na_tabela_devolve_nulo_e_nao_zero(todas_as_regioes: Config) -> None:
    """Competência em que um dígito não tem pessoa física nenhuma.

    Zero afirmaria colisão impossível, que é uma afirmação forte e falsa. Nulo
    diz que não há número para aquele dígito.
    """
    tabela = np.full(10, np.nan, dtype=np.float64)
    tabela[8] = 0.5
    _gravar_tabela(todas_as_regioes, tabela)
    cat = abrir_catalogo(todas_as_regioes)

    por_regiao = {
        cat.regiao_de(no): cat.taxa_de_colisao_de(no)
        for no in range(cat.nos)
        if cat.confianca_de(no) == "estimada"
    }

    assert por_regiao["8"] == 0.5
    assert all(taxa is None for digito, taxa in por_regiao.items() if digito != "8")


def test_a_tabela_precisa_ter_uma_posicao_por_digito(todas_as_regioes: Config) -> None:
    _gravar_tabela(todas_as_regioes, np.zeros(9, dtype=np.float64))

    with pytest.raises(ArtefatosIncompativeisError, match="dígito"):
        abrir_catalogo(todas_as_regioes)


def test_a_conferencia_da_derivacao_reprova_tabela_errada() -> None:
    """A validação que nunca reprovou não provou que sabe reprovar.

    A guarda roda na construção sobre os 5,6 milhões de nós de pessoa física, e
    aqui ela é exercitada contra um caso construído para falhar — senão "passou"
    não significaria nada.
    """
    tabela = np.array([0.5] + [np.nan] * 9, dtype=np.float64)
    regioes = np.array([0, 0], dtype=np.int8)
    aplica = np.array([True, True])
    esperado = np.array([0.5, 0.25])  # o segundo nó discorda da tabela

    with pytest.raises(TaxaDivergenteError, match="1"):
        _conferir_taxa(tabela, regioes, aplica, esperado)


def test_a_conferencia_da_derivacao_aprova_o_caso_fiel() -> None:
    """Controle positivo: a guarda tem de deixar passar o que está certo."""
    tabela = np.array([0.5] + [np.nan] * 9, dtype=np.float64)
    regioes = np.array([0, 0, -1], dtype=np.int8)
    aplica = np.array([True, True, False])
    esperado = np.array([0.5, 0.5, np.nan])

    _conferir_taxa(tabela, regioes, aplica, esperado)


def test_a_tabela_recusa_regiao_com_duas_taxas() -> None:
    """A derivação inteira depende de a taxa ser **função** da região. Isso é
    conferido, e não suposto."""
    regioes = np.array([3, 3], dtype=np.int8)
    taxas = np.array([0.5, 0.25])

    with pytest.raises(TaxaDivergenteError, match="região 3"):
        _tabela_de_taxas(regioes, taxas)


# ------------------------------------------------- ida e volta


def test_o_nome_volta_igual_ao_que_entrou(construido: Config) -> None:
    cat = abrir_catalogo(construido)

    nomes = {cat.nome_de(no) for no in range(cat.nos)}

    assert "ALFA COMERCIO LTDA" in nomes
    assert "BRAVO SERVICOS SA" in nomes
    assert None in nomes, "pessoa física não tem nome no artefato publicável"


def test_o_cnpj_leva_ao_no_e_o_no_de_volta_ao_cnpj(construido: Config) -> None:
    """A volta é derivada na abertura, e precisa concordar com a ida."""
    cat = abrir_catalogo(construido)

    for cnpj in (11111111, 22222222):
        indice = cat.indice_de(cnpj)
        assert indice is not None
        assert cat.cnpj_basico_de(indice) == f"{cnpj:08d}"


def test_o_zero_a_esquerda_do_cnpj_e_recomposto(tmp_path: Path) -> None:
    """O `cnpj_basico` vira int32 no catálogo; o zero volta na leitura."""
    config = Config(competencia="2026-06", data_dir=tmp_path, uf_alvo="SP")
    gravar_estabelecimentos(config, [estabelecimento("00111222"), estabelecimento("22222222")])
    aplicar_recorte_por_uf(config)
    gravar_empresas(
        config,
        [
            empresa("00111222", razao_social="ZERO A ESQUERDA LTDA"),
            empresa("22222222", razao_social="OUTRA LTDA"),
        ],
    )
    _gravar_dominio(config, "Naturezas", NATUREZAS_PADRAO)
    _gravar_dominio(config, "Qualificacoes", QUALIFICACOES_PADRAO)
    _gravar_dominio(config, "Paises", PAISES_PADRAO)
    tipar_empresas(config)
    gravar_socios(config, [socio("00111222", tipo="1", documento="22222222000199")])
    tipar_socios(config)
    gerar_identidades(config)
    gerar_nos(config)
    serializar_metadados(config)

    cat = abrir_catalogo(config)

    indice = cat.indice_de(111222)
    assert indice is not None
    assert cat.cnpj_basico_de(indice) == "00111222"


def test_empresa_que_nao_e_no_devolve_nada(construido: Config) -> None:
    """`33333333` está no recorte e não tem vínculo: existe, e não é nó.

    Quem responde existência é `existencia.npy`; o catálogo responde "é nó?".
    """
    cat = abrir_catalogo(construido)

    assert cat.indice_de(33333333) is None
    assert cat.indice_de(99999999) is None


def test_pessoa_fisica_nao_tem_cnpj(construido: Config) -> None:
    cat = abrir_catalogo(construido)

    fisicas = [no for no in range(cat.nos) if cat.tipo_de(no) == "pessoa_fisica"]
    assert fisicas
    assert all(cat.cnpj_basico_de(no) is None for no in fisicas)


def test_os_atributos_empacotados_voltam_certos(construido: Config) -> None:
    """Tipo, confiança e recorte dividem um byte. Trocar bit é trocar significado."""
    cat = abrir_catalogo(construido)

    indice = cat.indice_de(11111111)
    assert indice is not None
    assert cat.tipo_de(indice) == "pessoa_juridica"
    assert cat.confianca_de(indice) == "exata"
    assert cat.no_recorte_de(indice) is True

    fisica = next(no for no in range(cat.nos) if cat.tipo_de(no) == "pessoa_fisica")
    assert cat.confianca_de(fisica) == "estimada"
    assert cat.no_recorte_de(fisica) is None, "recorte é ternário: PF não é empresa"
    assert cat.regiao_de(fisica) == "8"
    assert cat.regiao_de(indice) is None, "empresa não tem região fiscal"


# ------------------------- o mecanismo que custou mil vezes


def test_a_busca_binaria_usa_o_tipo_do_array(
    construido: Config, monkeypatch: pytest.MonkeyPatch
) -> None:
    """`searchsorted` com `int` do Python promove o array inteiro a cada chamada.

    Medido sobre os 20 MB de `cnpj_ordenado`: 8.289 µs contra 8,0 µs. O resultado
    é o mesmo, nada falha, e a consulta fica mil vezes mais lenta — por isso o
    teste afirma o **mecanismo**, que não some, em vez do tempo, que varia com a
    máquina.
    """
    cat = abrir_catalogo(construido)
    vistos: list[Any] = []
    original = np.searchsorted

    def espiao(a: Any, v: Any, **kwargs: Any) -> Any:
        vistos.append((np.asarray(a).dtype, np.asarray(v).dtype))
        return original(a, v, **kwargs)

    monkeypatch.setattr(np, "searchsorted", espiao)
    cat.indice_de(11111111)

    assert vistos, "a busca precisa ter acontecido"
    assert all(array == alvo for array, alvo in vistos), (
        f"o alvo tem de ter o tipo do array: {vistos}"
    )


# --------------------------------------- arrays paralelos, de novo


def test_deslocamento_desalinhado_e_recusado() -> None:
    """A guarda de ida e volta, contra caso construído para reprovar.

    Um byte a mais no deslocamento devolve o nome de outra empresa. Nada muda de
    tamanho, nada falha, e a resposta sai plausível — é a terceira vez que este
    projeto encontra essa forma de defeito.
    """
    nomes = [b"ALFA", b"BRAVO", b"CHARLIE"]
    blob = zlib.compress(b"".join(nomes), 6)
    bloco_inicio = np.array([0, sum(len(n) for n in nomes)], dtype=np.int32)
    bloco_byte = np.array([0, len(blob)], dtype=np.int64)
    torto = np.array([0, 5, 9, 16], dtype=np.int32)  # certo seria 0, 4, 9, 16

    with pytest.raises(NomeDivergenteError, match="desalinhados"):
        _conferir_ida_e_volta(nomes, torto, blob, bloco_inicio, bloco_byte)


def test_deslocamento_alinhado_passa() -> None:
    """Controle positivo: sem ele a guarda poderia estar reprovando tudo."""
    nomes = [b"ALFA", b"BRAVO", b"CHARLIE"]
    blob = zlib.compress(b"".join(nomes), 6)
    bloco_inicio = np.array([0, sum(len(n) for n in nomes)], dtype=np.int32)
    bloco_byte = np.array([0, len(blob)], dtype=np.int64)
    certo = np.array([0, 4, 9, 16], dtype=np.int32)

    _conferir_ida_e_volta(nomes, certo, blob, bloco_inicio, bloco_byte)


def test_bloco_de_tamanho_divergente_e_recusado() -> None:
    nomes = [b"ALFA"]
    blob = zlib.compress(b"ALFA", 6)
    bloco_inicio = np.array([0, 99], dtype=np.int32)
    bloco_byte = np.array([0, len(blob)], dtype=np.int64)

    with pytest.raises(NomeDivergenteError, match="discordam"):
        _conferir_ida_e_volta(
            nomes, np.array([0, 4], dtype=np.int32), blob, bloco_inicio, bloco_byte
        )


def test_nenhum_nome_atravessa_fronteira_de_bloco(construido: Config) -> None:
    """O corte é entre nomes. Sem isso, ler um nome exigiria emendar dois blocos —
    e emenda é onde erro de fronteira se esconde."""
    cat = abrir_catalogo(construido)

    for no in range(cat.nos):
        inicio, fim = int(cat.nome_offsets[no]), int(cat.nome_offsets[no + 1])
        if inicio == fim:
            continue
        bloco = int(np.searchsorted(cat.bloco_inicio, np.int32(inicio), side="right")) - 1
        assert fim <= int(cat.bloco_inicio[bloco + 1])


# ---------------------------------------------------- as guardas


@pytest.mark.parametrize("ausente", ["atributos.npy", "nomes.bin", "bloco_byte.npy"])
def test_arquivo_ausente_diz_qual_falta(construido: Config, ausente: str) -> None:
    config = construido
    (config.data_dir / "grafo" / "2026-06" / ausente).unlink()

    with pytest.raises(ArtefatoAusenteError, match=ausente):
        abrir_catalogo(config)


def test_arrays_paralelos_de_tamanhos_diferentes_sao_recusados(construido: Config) -> None:
    config = construido
    destino = config.data_dir / "grafo" / "2026-06" / "no_por_cnpj.npy"
    with destino.open("wb") as arquivo:
        np.save(arquivo, np.array([0], dtype=np.int32), allow_pickle=False)

    with pytest.raises(ArtefatosIncompativeisError, match="arrays paralelos"):
        abrir_catalogo(config)


def test_blob_de_outra_execucao_e_recusado(construido: Config) -> None:
    config = construido
    (config.data_dir / "grafo" / "2026-06" / "nomes.bin").write_bytes(b"curto demais")

    with pytest.raises(ArtefatosIncompativeisError, match="execuções diferentes"):
        abrir_catalogo(config)


@pytest.mark.parametrize("indice", [-1, 999])
def test_no_fora_da_faixa_e_recusado(construido: Config, indice: int) -> None:
    cat = abrir_catalogo(construido)

    with pytest.raises(NoForaDaFaixaError, match="fora da faixa"):
        cat.nome_de(indice)


def test_sem_nos_a_mensagem_diz_o_que_falta(tmp_path: Path) -> None:
    config = Config(competencia="2026-06", data_dir=tmp_path, uf_alvo="SP")
    (tmp_path / "grafo" / "2026-06").mkdir(parents=True)

    with pytest.raises(NosAusentesError, match="conversão"):
        serializar_metadados(config)


def test_o_bloco_alvo_e_o_medido() -> None:
    """64 KiB saiu de medição: 32 dão 66,9 MB, 64 dão 63,6 MB, 256 dão 60,8 MB."""
    assert BLOCO == 64 * 1024


# ------------------------- a fronteira entre serving e construção


def test_o_catalogo_nao_carrega_motor_nem_leitor_de_parquet() -> None:
    """O motivo de o formato existir, afirmado em vez de documentado.

    Se o catálogo arrastasse pyarrow ou DuckDB, ele não teria razão de ser: o
    Parquet já estava lá.
    """
    codigo = (
        "import sys; import grafo_societario.graph.catalogo; "
        "print('duckdb' in sys.modules, 'scipy' in sys.modules, 'pyarrow' in sys.modules)"
    )

    saida = subprocess.run(
        [sys.executable, "-c", codigo], capture_output=True, text=True, check=True
    )

    assert saida.stdout.strip() == "False False False", saida.stdout
    assert modulo.__name__.endswith("catalogo")
