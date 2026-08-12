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
            for linha in conexao.execute("SELECT * FROM n").fetchall()
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


def test_nao_existe_coluna_de_indice(silver: Config) -> None:
    """O índice é a posição da linha. Gravá-lo custava 27,8 MiB para repetir o
    número da linha em que o número já estava."""
    resultado = gerar_nos(silver)

    with duckdb.connect() as conexao:
        colunas = {
            linha[0]
            for linha in conexao.execute(
                f"DESCRIBE SELECT * FROM read_parquet('{resultado.caminho.as_posix()}')"
            ).fetchall()
        }
    assert "indice" not in colunas


def test_o_arquivo_esta_ordenado_por_identificador(silver: Config) -> None:
    """É o que torna a posição da linha um índice: sem ordem estrita, a linha k
    deixa de ser o nó k e o CSR passa a endereçar outro nó."""
    resultado = gerar_nos(silver)

    identificadores = [str(linha["identificador"]) for linha in ler_nos(resultado.caminho)]
    assert identificadores == sorted(identificadores)
    assert len(set(identificadores)) == resultado.nos


def gravar_identificadores(caminho: Path, valores: str) -> Path:
    with duckdb.connect() as conexao:
        conexao.execute(
            f"COPY (SELECT * FROM (VALUES {valores}) AS t(identificador)) "
            f"TO '{caminho.as_posix()}' (FORMAT PARQUET)"
        )
    return caminho


@pytest.mark.parametrize(
    ("valores", "defeito"),
    [
        ("('bb'), ('aa'), ('cc')", "fora de ordem: a linha k não é o nó k"),
        ("('aa'), ('aa'), ('bb')", "repetido: dois nós na mesma posição"),
        ("('cc'), ('bb'), ('aa')", "decrescente"),
    ],
)
def test_posicao_que_nao_e_indice_denso_e_recusada(
    tmp_path: Path, valores: str, defeito: str
) -> None:
    """A guarda exercitada contra casos construídos para reprovar."""
    caminho = gravar_identificadores(tmp_path / "nos.parquet", valores)

    with duckdb.connect() as conexao, pytest.raises(IndiceNaoDensoError, match="linha k"):
        validar_indice_denso(conexao, caminho)


def test_arquivo_ordenado_passa_pela_guarda(tmp_path: Path) -> None:
    """Controle positivo: sem ele, a guarda poderia estar reprovando tudo."""
    caminho = gravar_identificadores(tmp_path / "nos.parquet", "('aa'), ('bb'), ('cc')")

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


# ------------------------------------- nome de pessoa física e o artefato


def test_artefato_publicavel_nao_carrega_nome_de_pessoa_fisica(silver: Config) -> None:
    """`nos.parquet` vai para Release e para imagem Docker.

    São 5,6 milhões de nós que são gente. Pseudonimizar na resposta da API não
    desfaria nada — é o mesmo argumento que moveu a supressão de CPF para a
    transformação, aplicado ao campo vizinho: o que entra no artefato já saiu.
    """
    assert silver.expor_pf is False, "o padrão é o modo publicável"

    resultado = gerar_nos(silver)

    por_tipo: dict[str, list[object]] = {}
    for linha in ler_nos(resultado.caminho):
        por_tipo.setdefault(str(linha["tipo"]), []).append(linha["nome"])
    assert all(nome is None for nome in por_tipo["pessoa_fisica"])
    assert resultado.expor_pf is False


def test_artefato_publicavel_nao_carrega_a_mascara_do_cpf(silver: Config) -> None:
    """A máscara é chave de junção de volta ao `Socios` da Receita.

    Com `***123456**` e a empresa de que o nó é sócio, recupera-se o nome na fonte
    original, que é pública. Nó pseudonimizado que carrega a chave de busca não
    está pseudonimizado.
    """
    resultado = gerar_nos(silver)

    fisicas = [linha for linha in ler_nos(resultado.caminho) if linha["tipo"] == "pessoa_fisica"]
    assert fisicas, "a fixture precisa ter pessoa física"
    assert all(linha["cpf_mascarado"] is None for linha in fisicas)


def test_regiao_fiscal_substitui_a_mascara(silver: Config) -> None:
    """Um dígito no lugar de seis: a funcionalidade fica, a identificabilidade sai.

    A taxa de colisão por nó depende só da região fiscal, então é ela que precisa
    ser publicável — e sozinha ela não junta com nada.
    """
    resultado = gerar_nos(silver)

    fisicas = [linha for linha in ler_nos(resultado.caminho) if linha["tipo"] == "pessoa_fisica"]
    assert [linha["regiao_fiscal"] for linha in fisicas] == ["8"]
    assert all(linha["taxa_de_colisao"] is not None for linha in fisicas)


def test_regiao_fiscal_permanece_com_expor_pf(silver: Config) -> None:
    """O esquema é o mesmo nos dois modos: consumidor que lê o artefato local não
    pode precisar de outro código para ler o publicado."""
    local = silver.model_copy(update={"expor_pf": True})

    resultado = gerar_nos(local)

    fisicas = [linha for linha in ler_nos(resultado.caminho) if linha["tipo"] == "pessoa_fisica"]
    assert fisicas[0]["cpf_mascarado"] == "***123458**"
    assert fisicas[0]["regiao_fiscal"] == "8"


def test_pessoa_juridica_nao_tem_regiao_fiscal(silver: Config) -> None:
    """Região fiscal é dígito de CPF. Empresa não tem, e inventar seria pior que
    devolver nulo."""
    resultado = gerar_nos(silver)

    juridicas = [
        linha for linha in ler_nos(resultado.caminho) if linha["tipo"] == "pessoa_juridica"
    ]
    assert all(linha["regiao_fiscal"] is None for linha in juridicas)


def test_razao_social_de_pessoa_juridica_permanece(silver: Config) -> None:
    """A distinção não mudou: nome legal do negócio sai em nota fiscal e no cartão
    CNPJ. Apagá-lo custaria utilidade sem comprar privacidade."""
    resultado = gerar_nos(silver)

    por_identificador = {str(linha["identificador"]): linha for linha in ler_nos(resultado.caminho)}
    assert por_identificador[no_da_empresa("11111111")]["nome"] == "ALFA LTDA"


def test_estrangeiro_conta_como_pessoa_fisica(tmp_path: Path) -> None:
    """Estrangeiro entra na regra por ser pessoa, e não por ter documento — ele
    não tem nenhum."""
    config = Config(competencia="2026-06", data_dir=tmp_path, uf_alvo="SP")
    gravar_estabelecimentos(config, [estabelecimento("11111111")])
    aplicar_recorte_por_uf(config)
    gravar_empresas(config, [empresa("11111111", razao_social="ALFA LTDA")])
    _gravar_dominio(config, "Naturezas", NATUREZAS_PADRAO)
    _gravar_dominio(config, "Qualificacoes", QUALIFICACOES_PADRAO)
    _gravar_dominio(config, "Paises", PAISES_PADRAO)
    tipar_empresas(config)
    gravar_socios(
        config, [socio("11111111", tipo="3", nome="ECHO FOXTROT", documento="", pais="249")]
    )
    tipar_socios(config)
    gerar_identidades(config)

    resultado = gerar_nos(config)

    estrangeiros = [linha for linha in ler_nos(resultado.caminho) if linha["tipo"] == "estrangeiro"]
    assert len(estrangeiros) == 1
    assert estrangeiros[0]["nome"] is None


def test_expor_pf_verdadeiro_gera_artefato_local_com_nomes(silver: Config) -> None:
    """O outro modo, sem o qual a flag não estaria realmente decidindo nada.

    Quem roda local sobre os dados originais precisa dos nomes: o código é aberto
    justamente para isso. O que não acontece é o artefato **publicado** carregá-los.
    """
    local = silver.model_copy(update={"expor_pf": True})

    resultado = gerar_nos(local)

    fisicas = [linha for linha in ler_nos(resultado.caminho) if linha["tipo"] == "pessoa_fisica"]
    assert [linha["nome"] for linha in fisicas] == ["FULANO DE TAL"]
    assert resultado.expor_pf is True


def test_sem_silver_a_mensagem_diz_o_que_falta(tmp_path: Path) -> None:
    config = Config(competencia="2026-06", data_dir=tmp_path, uf_alvo="SP")
    (tmp_path / "silver" / "2026-06").mkdir(parents=True)

    with pytest.raises(SilverAusenteError, match="recorte"):
        gerar_nos(config)
