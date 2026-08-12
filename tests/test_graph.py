"""Nós do grafo: quem entra, quem só existe, e por que o índice não sai na API.

O caso central destes testes é a distinção que o desenho existe para preservar:
empresa **sem vínculo** e empresa **inexistente** têm de dar respostas diferentes,
e a segunda seria mentira sobre 14,79 milhões de empresas do recorte.
"""

from __future__ import annotations

from pathlib import Path

import duckdb
import numpy as np
import pytest

from grafo_societario.config import Config
from grafo_societario.graph.build import (
    COLUNAS_NOS,
    ExistenciaDesordenadaError,
    IndiceNaoDensoError,
    SilverAusenteError,
    gerar_nos,
    validar_existencia_ordenada,
    validar_indice_denso,
)
from grafo_societario.transform.identity import gerar_identidades, identificador
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

RECORTE = ["11111111", "22222222", "33333333", "44444444"]


@pytest.fixture
def silver(tmp_path: Path) -> Config:
    """Quatro empresas no recorte, cobrindo os quatro casos que importam.

    - `11111111` tem sócios: pessoa física e a empresa `22222222`.
    - `22222222` **não tem sócio nenhum** e mesmo assim tem aresta, por ser sócia.
      São 1.311 casos assim no recorte real, e contá-los pelo lado errado os
      deixaria fora do grafo com grau aparente zero.
    - `33333333` tem como sócia uma empresa **de fora** do recorte.
    - `44444444` não tem vínculo nenhum: existe, e não é nó.
    """
    config = Config(competencia="2026-06", data_dir=tmp_path, uf_alvo="SP")
    gravar_estabelecimentos(config, [estabelecimento(cnpj) for cnpj in RECORTE])
    aplicar_recorte_por_uf(config)

    gravar_empresas(
        config,
        [
            empresa("11111111", razao_social="ALFA LTDA"),
            empresa("22222222", razao_social="BRAVO LTDA"),
            empresa("33333333", razao_social="CHARLIE LTDA"),
            empresa("44444444", razao_social="DELTA LTDA"),
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
            socio("11111111", tipo="1", nome="BRAVO LIMITADA", documento="22222222000199"),
            socio("33333333", tipo="1", nome="ECHO DE FORA SA", documento="99999999000199"),
        ],
    )
    tipar_socios(config)
    gerar_identidades(config)
    return config


def ler_nos(caminho: Path) -> list[dict[str, object]]:
    with duckdb.connect() as conexao:
        conexao.execute(f"CREATE VIEW n AS SELECT * FROM read_parquet('{caminho.as_posix()}')")
        colunas = [linha[0] for linha in conexao.execute("DESCRIBE n").fetchall()]
        return [
            dict(zip(colunas, linha, strict=True))
            for linha in conexao.execute("SELECT * FROM n ORDER BY indice").fetchall()
        ]


def no_da_empresa(cnpj_basico: str) -> str:
    return identificador("pessoa_juridica", cnpj_basico)


# ------------------------------- a fronteira entre estar no grafo e existir


def test_empresa_sem_vinculo_fica_fora_do_grafo(silver: Config) -> None:
    """Nó de grau zero não pode estar em caminho nenhum, e carregá-lo custa
    `indptr` e metadado por nada — é o que estoura o orçamento de 500 MB."""
    resultado = gerar_nos(silver)

    identificadores = {str(linha["identificador"]) for linha in ler_nos(resultado.caminho)}
    assert no_da_empresa("44444444") not in identificadores
    assert resultado.isolados == 1


def test_empresa_sem_vinculo_continua_existindo(silver: Config) -> None:
    """A outra metade do desenho, e a que impede uma mentira.

    "Não tem vínculo" e "não existe" são respostas diferentes. Se ficar de fora do
    grafo fizesse a consulta responder "não encontrada", o projeto estaria negando
    a existência de 74,8% das empresas do recorte.
    """
    resultado = gerar_nos(silver)

    existencia = np.load(resultado.caminho_da_existencia)
    assert resultado.existencia == len(RECORTE)
    for cnpj in RECORTE:
        assert int(cnpj) in existencia, f"{cnpj} existe e precisa ser respondível"


def test_socia_sem_socios_entra_no_grafo(silver: Config) -> None:
    """Uma empresa pode ter aresta sem ter sócio: basta ser sócia de outra.

    São 1.311 no recorte real. Contar só quem tem sócio as deixaria fora do grafo
    com grau aparente zero, e elas têm vínculo.
    """
    resultado = gerar_nos(silver)

    identificadores = {str(linha["identificador"]) for linha in ler_nos(resultado.caminho)}
    assert no_da_empresa("22222222") in identificadores


def test_socia_de_fora_do_recorte_entra_marcada(silver: Config) -> None:
    """Conector: entra no grafo, e não conta como empresa do recorte."""
    resultado = gerar_nos(silver)

    por_identificador = {str(linha["identificador"]): linha for linha in ler_nos(resultado.caminho)}
    externa = por_identificador[no_da_empresa("99999999")]
    assert externa["no_recorte"] is False
    assert por_identificador[no_da_empresa("11111111")]["no_recorte"] is True

    existencia = np.load(resultado.caminho_da_existencia)
    assert 99999999 not in existencia, "conector não é empresa do recorte"


# ------------------------------------------------------- o índice denso


def test_indice_cobre_zero_ate_n_menos_um(silver: Config) -> None:
    resultado = gerar_nos(silver)

    indices = [int(str(linha["indice"])) for linha in ler_nos(resultado.caminho)]
    assert indices == list(range(resultado.nos))


def test_indice_e_int32_e_nao_int64(silver: Config) -> None:
    """São 10,6 milhões de nós contra um teto de 2,1 bilhões. O dobro de largura
    custaria 40 MiB no `indptr` sem comprar nada."""
    resultado = gerar_nos(silver)

    with duckdb.connect() as conexao:
        tipo = conexao.execute(
            f"SELECT column_type FROM (DESCRIBE SELECT * FROM "
            f"read_parquet('{resultado.caminho.as_posix()}')) WHERE column_name = 'indice'"
        ).fetchone()
    assert tipo is not None
    assert tipo[0] == "INTEGER"


def gravar_indices(caminho: Path, valores: str) -> Path:
    with duckdb.connect() as conexao:
        conexao.execute(
            f"COPY (SELECT * FROM (VALUES {valores}) AS t(indice)) "
            f"TO '{caminho.as_posix()}' (FORMAT PARQUET)"
        )
    return caminho


@pytest.mark.parametrize(
    ("valores", "defeito"),
    [
        ("(0), (1), (3)", "buraco: 2 não existe"),
        ("(0), (1), (1)", "repetição: dois nós na mesma posição"),
        ("(1), (2), (3)", "não começa em zero"),
    ],
)
def test_indice_nao_denso_e_recusado(tmp_path: Path, valores: str, defeito: str) -> None:
    """A guarda exercitada contra casos construídos para reprovar.

    Buraco faz o CSR endereçar posição que não existe; repetição faz dois nós
    dividirem a mesma lista de vizinhos. Nos dois casos o sintoma é caminho
    societário errado, e não exceção.
    """
    caminho = gravar_indices(tmp_path / "nos.parquet", valores)

    with duckdb.connect() as conexao, pytest.raises(IndiceNaoDensoError, match="sem buraco"):
        validar_indice_denso(conexao, caminho)


def test_indice_denso_passa_pela_guarda(tmp_path: Path) -> None:
    """Controle positivo: sem ele, a guarda poderia estar reprovando tudo."""
    caminho = gravar_indices(tmp_path / "nos.parquet", "(0), (1), (2)")

    with duckdb.connect() as conexao:
        validar_indice_denso(conexao, caminho)


def test_o_indice_e_estavel_entre_execucoes(silver: Config) -> None:
    """A chave de tudo que vem depois. Se mudar entre execuções sobre o mesmo
    silver, o artefato deixa de ser imutável e a Fase 4 perde o chão."""
    primeiro = gerar_nos(silver)
    bytes_dos_nos = primeiro.caminho.read_bytes()
    bytes_da_existencia = primeiro.caminho_da_existencia.read_bytes()

    segundo = gerar_nos(silver)

    assert segundo.caminho.read_bytes() == bytes_dos_nos
    assert segundo.caminho_da_existencia.read_bytes() == bytes_da_existencia


# ---------------------------------------------------- o array de existência


def test_existencia_e_estritamente_crescente(silver: Config) -> None:
    """Busca binária sobre array desordenado não erra em voz alta: devolve
    "não existe" para quem existe."""
    resultado = gerar_nos(silver)

    existencia = np.load(resultado.caminho_da_existencia)
    assert existencia.dtype == np.int32
    assert bool(np.all(existencia[:-1] < existencia[1:]))


@pytest.mark.parametrize(
    ("valores", "defeito"),
    [
        ([3, 1, 2], "fora de ordem"),
        ([1, 2, 2, 3], "repetido, e a busca binária pressupõe unicidade"),
        ([9, 8, 7], "decrescente"),
    ],
)
def test_existencia_desordenada_e_recusada(valores: list[int], defeito: str) -> None:
    """Array desordenado não faz a busca binária falhar — faz ela mentir."""
    with pytest.raises(ExistenciaDesordenadaError, match="busca binária"):
        validar_existencia_ordenada(np.array(valores, dtype=np.int32))


@pytest.mark.parametrize("valores", [[], [7], [1, 2, 3]])
def test_existencia_ordenada_passa(valores: list[int]) -> None:
    """Controle positivo, inclusive nos extremos de zero e de um elemento."""
    validar_existencia_ordenada(np.array(valores, dtype=np.int32))


def test_busca_binaria_responde_existencia(silver: Config) -> None:
    """O uso a que o array se destina, exercitado — inclusive o caso negativo."""
    resultado = gerar_nos(silver)
    existencia = np.load(resultado.caminho_da_existencia)

    def existe(cnpj_basico: str) -> bool:
        posicao = int(np.searchsorted(existencia, int(cnpj_basico)))
        return posicao < existencia.size and int(existencia[posicao]) == int(cnpj_basico)

    assert existe("44444444"), "sem vínculo, mas existe"
    assert not existe("99999999"), "conector de fora não é empresa do recorte"
    assert not existe("55555555"), "nunca esteve no recorte"


# ------------------------------------------------------ metadados e esquema


def test_nome_vem_de_empresas_e_nao_da_grafia_do_socio(silver: Config) -> None:
    """`BRAVO LTDA` é a razão social tipada; `BRAVO LIMITADA` é como quem preencheu
    o quadro societário escreveu. A autoritativa é a primeira."""
    resultado = gerar_nos(silver)

    por_identificador = {str(linha["identificador"]): linha for linha in ler_nos(resultado.caminho)}
    assert por_identificador[no_da_empresa("22222222")]["nome"] == "BRAVO LTDA"


def test_esquema_dos_nos_e_o_declarado(silver: Config) -> None:
    resultado = gerar_nos(silver)

    with duckdb.connect() as conexao:
        descricao = conexao.execute(
            f"DESCRIBE SELECT * FROM read_parquet('{resultado.caminho.as_posix()}')"
        ).fetchall()
    assert tuple(coluna[0] for coluna in descricao) == COLUNAS_NOS


def test_pessoa_fisica_leva_a_taxa_de_colisao_para_o_no(silver: Config) -> None:
    """A confiança da identidade acompanha o nó, para a API poder respondê-la por
    nó em vez de publicar uma média que esconde a diferença entre regiões."""
    resultado = gerar_nos(silver)

    fisicas = [linha for linha in ler_nos(resultado.caminho) if linha["tipo"] == "pessoa_fisica"]
    assert len(fisicas) == 1
    assert fisicas[0]["confianca"] == "estimada"
    assert fisicas[0]["taxa_de_colisao"] is not None


def test_sem_silver_a_mensagem_diz_o_que_falta(tmp_path: Path) -> None:
    config = Config(competencia="2026-06", data_dir=tmp_path, uf_alvo="SP")
    (tmp_path / "silver" / "2026-06").mkdir(parents=True)

    with pytest.raises(SilverAusenteError, match="recorte"):
        gerar_nos(config)
