"""Normalização de nome: o que ela apaga, o que ela preserva, e onde ela para.

Os testes estão agrupados pelos três tipos de degrau que o módulo distingue.
Os do tipo 3 são os mais importantes: eles não testam o que a normalização faz,
testam o que ela **se recusa** a fazer. Sem eles, acrescentar "remove inicial do
meio" passaria na suíte inteira.
"""

from __future__ import annotations

from pathlib import Path

import duckdb
import pytest

from grafo_societario.config import Config
from grafo_societario.transform.identity import (
    COLUNAS_IDENTIDADES,
    ESTIMADA,
    EXATA,
    FRACA,
    NAO_FUNDIVEL,
    PARTICULAS,
    Identidades,
    SociosAusentesError,
    gerar_identidades,
    identificador,
    instalar_identificador,
    instalar_normalizacao,
    normalizar_nome,
)
from grafo_societario.transform.silver import tipar_socios
from test_silver import preparar_socios, socio

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
        ("ALFA DA BRAVO", "ALFA BRAVO"),
        ("CHARLIE DELTA DOS ECHO", "CHARLIE DELTA ECHO"),
        ("FOXTROT GOLF DA HOTEL", "FOXTROT GOLF HOTEL"),
        ("INDIA JULIETT DE KILO", "INDIA JULIETT KILO"),
        ("LIMA DOS MIKE NOVEMBER", "LIMA MIKE NOVEMBER"),
        ("OSCAR DI PAPA", "OSCAR PAPA"),
        ("QUEBEC E ROMEU SIERRA", "QUEBEC ROMEU SIERRA"),
        ("TANGO DAS UNIFORM", "TANGO UNIFORM"),
        ("DA VICTOR", "VICTOR"),
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


def test_os_sete_formatos_que_o_degrau_funde() -> None:
    """Os sete formatos de par que a remoção de partícula funde.

    Sete fusões foram medidas sobre 5.635.007 identidades do recorte de SP na
    competência 2026-06, todas com a mesma máscara de CPF dentro do par — a mesma
    pessoa digitada de dois jeitos. É a evidência direta que sustenta aceitar o
    degrau, mais forte que o argumento probabilístico.

    **Os nomes reais não são reproduzidos.** São sete pessoas físicas
    identificáveis, e travá-las nominalmente num repositório público seria
    singularizá-las para sempre no histórico — exatamente o que a fixture de
    Socios foi anonimizada para evitar, e o oposto do que o README promete.

    Os pares abaixo são sintéticos e reproduzem **os mesmos sete padrões**: `DA`
    em terceira e em segunda posição, `DE` em terceira e em segunda, `DOS` em
    terceira e em segunda, e a ausência do lado oposto. O que o teste prova — que
    a regra funde estes formatos — continua provado.
    """
    pares = [
        ("ALFA BRAVO DA CHARLIE", "ALFA BRAVO CHARLIE"),
        ("DELTA DA ECHO", "DELTA ECHO"),
        ("FOXTROT GOLF DE HOTEL", "FOXTROT GOLF HOTEL"),
        ("INDIA JULIETT KILO", "INDIA JULIETT DOS KILO"),
        ("LIMA MIKE NOVEMBER", "LIMA MIKE DA NOVEMBER"),
        ("OSCAR PAPA QUEBEC", "OSCAR DE PAPA QUEBEC"),
        ("ROMEU DOS SIERRA TANGO", "ROMEU SIERRA TANGO"),
    ]
    assert len(pares) == 7
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
    assert normalizar_nome("ALFA BRAVO CHARLIE") != normalizar_nome("ALFA CHARLIE")


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


# ====================================================== identificador estável


@pytest.fixture
def config_de_identidade(tmp_path: Path) -> Config:
    return Config(competencia="2026-06", data_dir=tmp_path, uf_alvo="SP")


def gerar(config: Config, registros: list[dict[str, str]], **extras: object) -> Identidades:
    preparar_socios(config, registros, **extras)  # type: ignore[arg-type]
    tipar_socios(config)
    return gerar_identidades(config)


def ler(caminho: Path) -> dict[str, dict[str, object]]:
    with duckdb.connect() as conexao:
        conexao.execute(f"CREATE VIEW i AS SELECT * FROM read_parquet('{caminho.as_posix()}')")
        colunas = [linha[0] for linha in conexao.execute("DESCRIBE i").fetchall()]
        linhas = conexao.execute("SELECT * FROM i").fetchall()
    return {
        str(dict(zip(colunas, linha, strict=True))["identificador"]): dict(
            zip(colunas, linha, strict=True)
        )
        for linha in linhas
    }


# ------------------------------------------------ o critério de aceite do plano


def test_mesmo_socio_em_duas_empresas_gera_o_mesmo_id(config_de_identidade: Config) -> None:
    """O critério que define se a identidade serve para alguma coisa.

    Sem ele não há grafo: cada participação viraria uma pessoa diferente, e
    caminho societário entre duas empresas nunca existiria.
    """
    resultado = gerar(
        config_de_identidade,
        [
            socio("11111111", nome="FULANO DE TAL", documento="***123456**"),
            socio("22222222", nome="FULANO DE TAL", documento="***123456**"),
        ],
    )

    identidades = ler(resultado.caminho)
    pessoas = [linha for linha in identidades.values() if linha["tipo"] == "pessoa_fisica"]
    assert len(pessoas) == 1
    assert pessoas[0]["vinculos_no_recorte"] == 2


def test_a_normalizacao_alcanca_a_identidade(config_de_identidade: Config) -> None:
    """Grafias que normalizam igual são a mesma pessoa, com a mesma máscara."""
    resultado = gerar(
        config_de_identidade,
        [
            socio("11111111", nome="ALFA BRAVO DA CHARLIE", documento="***123456**"),
            socio("22222222", nome="alfa bravo charlie", documento="***123456**"),
        ],
    )

    pessoas = [
        linha for linha in ler(resultado.caminho).values() if linha["tipo"] == "pessoa_fisica"
    ]
    assert len(pessoas) == 1
    assert pessoas[0]["vinculos_no_recorte"] == 2


def test_mesma_mascara_com_nomes_diferentes_sao_duas_pessoas(
    config_de_identidade: Config,
) -> None:
    """O outro lado: a máscara sozinha não identifica, e o nome é quem separa.

    São 48,8 pessoas por máscara na região fiscal de SP. Sem o nome, todas elas
    seriam um nó só.
    """
    resultado = gerar(
        config_de_identidade,
        [
            socio("11111111", nome="FULANO DE TAL", documento="***123456**"),
            socio("22222222", nome="DELTA ECHO", documento="***123456**"),
        ],
    )

    pessoas = [
        linha for linha in ler(resultado.caminho).values() if linha["tipo"] == "pessoa_fisica"
    ]
    assert len(pessoas) == 2


# ------------------------------------------------------- os quatro caminhos


def test_pessoa_juridica_identifica_pelo_cnpj_basico(config_de_identidade: Config) -> None:
    """Grafia do nome não muda a identidade de PJ: o cnpj_basico é exato."""
    resultado = gerar(
        config_de_identidade,
        [
            socio("11111111", tipo="1", nome="ACME LTDA", documento="99999999000199"),
            socio("22222222", tipo="1", nome="ACME LIMITADA", documento="99999999000280"),
        ],
    )

    juridicas = [
        linha for linha in ler(resultado.caminho).values() if linha["tipo"] == "pessoa_juridica"
    ]
    assert len(juridicas) == 1
    assert juridicas[0]["cnpj_basico"] == "99999999"
    assert juridicas[0]["confianca"] == EXATA
    assert juridicas[0]["taxa_de_colisao"] is None, "identidade exata não tem taxa a estimar"


def test_estrangeiro_identifica_por_nome_e_pais(config_de_identidade: Config) -> None:
    """Terceiro caminho: sem documento nenhum, sobra nome e país. Ver ADR-004."""
    resultado = gerar(
        config_de_identidade,
        [
            socio("11111111", tipo="3", nome="ECHO FOXTROT", documento="", pais="249"),
            socio("22222222", tipo="3", nome="ECHO FOXTROT", documento="", pais="105"),
        ],
        paises={"105": "BRASIL", "249": "ESTADOS UNIDOS"},
    )

    estrangeiros = [
        linha for linha in ler(resultado.caminho).values() if linha["tipo"] == "estrangeiro"
    ]
    assert len(estrangeiros) == 2, "mesmo nome em países diferentes não é a mesma pessoa"
    assert {str(linha["confianca"]) for linha in estrangeiros} == {FRACA}


def test_socio_sem_nome_nao_funde_com_ninguem(config_de_identidade: Config) -> None:
    """370 vínculos do recorte de SP não têm nome.

    A identidade seria a máscara sozinha, que funde os 48,8 portadores médios de
    uma máscara da região 8. Cada registro vira um nó — isolar custa nada, fundir
    é uma afirmação falsa sobre quem é sócio de quem.
    """
    resultado = gerar(
        config_de_identidade,
        [
            socio("11111111", nome="", documento="***123456**"),
            socio("22222222", nome="", documento="***123456**"),
            socio("33333333", nome="FULANO DE TAL", documento="***123456**"),
        ],
    )

    identidades = ler(resultado.caminho)
    sem_nome = [linha for linha in identidades.values() if linha["confianca"] == NAO_FUNDIVEL]
    assert len(sem_nome) == 2, "mesma máscara, sem nome, empresas diferentes: dois nós"
    assert all(linha["vinculos_no_recorte"] == 1 for linha in sem_nome)
    assert dict(resultado.por_confianca)[NAO_FUNDIVEL] == 2


def test_tipos_diferentes_nunca_colidem() -> None:
    """A etiqueta de tipo entra no hash para que um cnpj_basico e um nome que por
    acaso sejam a mesma string não virem o mesmo nó."""
    assert identificador("pessoa_juridica", "12345678") != identificador(
        "pessoa_fisica", "12345678"
    )


# --------------------------------------------- nó externo: conector, não nó


def test_socio_juridico_de_fora_entra_marcado(config_de_identidade: Config) -> None:
    """19% dos vínculos entre empresas apontam para fora do recorte.

    Descartá-los quebraria caminho real: duas paulistas ligadas por uma holding
    de outra UF deixariam de ter vínculo. Eles entram como conector, e a marca é
    o que impede alguém de ler o grau deles como o grau real.
    """
    resultado = gerar(
        config_de_identidade,
        [
            socio("11111111", tipo="1", nome="HOTEL INDIA SA", documento="77777777000199"),
            socio("11111111", tipo="1", nome="ACME SP LTDA", documento="22222222000199"),
        ],
        no_recorte=["11111111", "22222222"],
    )

    juridicas = {
        str(linha["cnpj_basico"]): linha
        for linha in ler(resultado.caminho).values()
        if linha["tipo"] == "pessoa_juridica"
    }
    assert juridicas["77777777"]["no_recorte"] is False
    assert juridicas["22222222"]["no_recorte"] is True
    assert resultado.externos == 1


def test_vinculos_no_recorte_nao_se_chama_grau(config_de_identidade: Config) -> None:
    """O nome da coluna é o que impede a leitura errada.

    Só ingerimos sócios de empresas do recorte, então quem tem 3 participações em
    SP e 40 fora aparece com 3. É piso, nunca total — e vale para a Fase 5, onde
    grau e centralidade são "dentro do recorte", e para a Fase 9, onde "pessoa com
    participação em N empresas" é afirmação falsa se N for lido como total.
    """
    assert "vinculos_no_recorte" in COLUNAS_IDENTIDADES
    assert "grau" not in COLUNAS_IDENTIDADES


# ------------------------------------------------------- taxa e estabilidade


def test_taxa_de_colisao_so_existe_onde_e_calculavel(config_de_identidade: Config) -> None:
    resultado = gerar(
        config_de_identidade,
        [
            socio("11111111", nome="FULANO DE TAL", documento="***123458**"),
            socio("22222222", tipo="1", nome="ACME LTDA", documento="99999999000199"),
            socio("33333333", tipo="3", nome="ECHO FOXTROT", documento="", pais="249"),
        ],
        paises={"105": "BRASIL", "249": "ESTADOS UNIDOS"},
    )

    por_confianca = {
        str(linha["confianca"]): linha["taxa_de_colisao"]
        for linha in ler(resultado.caminho).values()
    }
    assert por_confianca[ESTIMADA] is not None
    assert por_confianca[EXATA] is None
    assert por_confianca[FRACA] is None


def test_taxa_de_colisao_e_o_simpson_vezes_os_pares_homonimos(
    config_de_identidade: Config,
) -> None:
    """O cálculo conferido contra um caso computável à mão.

    Quatro identidades na região 8, uma máscara repetida e um par homônimo:

        soma de quadrados = 2² + 1² + 1² = 6      colisão = 6 / 4² = 0,375
        pares homônimos   = C(2,2) = 1           fusões  = 1 x 0,375
        taxa              = 0,375 / 4 = 0,09375

    As somas são inteiras de propósito: `double` não é associativo, o motor agrega
    em paralelo, e somar em ponto flutuante fazia o Parquet sair com bytes
    diferentes a cada execução sobre o mesmo dado.
    """
    resultado = gerar(
        config_de_identidade,
        [
            socio("11111111", nome="FULANO DE TAL", documento="***111118**"),
            socio("22222222", nome="FULANO DE TAL", documento="***222218**"),
            socio("33333333", nome="DELTA ECHO", documento="***111118**"),
            socio("44444444", nome="GOLF HOTEL", documento="***333318**"),
        ],
    )

    taxas = [
        linha["taxa_de_colisao"]
        for linha in ler(resultado.caminho).values()
        if linha["confianca"] == ESTIMADA
    ]
    assert len(taxas) == 4
    assert all(taxa == pytest.approx(0.09375) for taxa in taxas)
    assert resultado.fusoes_estimadas == pytest.approx(0.375)


def test_cada_regiao_fiscal_tem_a_sua_taxa(config_de_identidade: Config) -> None:
    """Média única esconderia que o risco é vinte vezes maior na região 8.

    Publicar a média tendo a distribuição é desperdiçar informação já medida, e é
    o que permite a API dizer a confiança do nó, e não a do conjunto.
    """
    resultado = gerar(
        config_de_identidade,
        [
            socio("11111111", nome="FULANO DE TAL", documento="***111118**"),
            socio("22222222", nome="FULANO DE TAL", documento="***222218**"),
            socio("33333333", nome="DELTA ECHO", documento="***111118**"),
            socio("44444444", nome="GOLF HOTEL", documento="***444443**"),
        ],
    )

    por_regiao = dict(resultado.taxa_por_regiao)
    assert set(por_regiao) == {"8", "3"}
    assert por_regiao["8"] > por_regiao["3"]
    assert por_regiao["3"] == 0.0, "região com uma identidade só não tem par homônimo"


def test_identificador_e_estavel_entre_execucoes(config_de_identidade: Config) -> None:
    """A Fase 4 indexa por este valor e a Fase 8 promete artefato imutável."""
    primeiro = gerar(config_de_identidade, [socio("11111111", nome="FULANO DE TAL")])
    segundo = gerar_identidades(config_de_identidade)

    assert primeiro.caminho.read_bytes() == segundo.caminho.read_bytes()


@pytest.mark.parametrize(
    "partes",
    [
        ("pessoa_fisica", "FULANO DE TAL", "***123456**"),
        ("pessoa_juridica", "12345678"),
        ("estrangeiro", "ECHO FOXTROT", "249"),
        ("nao_fundivel", "11111111", "***123456**", "", ""),
    ],
)
def test_macro_de_identificador_concorda_com_python(partes: tuple[str, ...]) -> None:
    """Mesma disciplina da normalização: duas implementações, uma regra."""
    lista = ", ".join(f"'{parte}'" for parte in partes)
    with duckdb.connect() as conexao:
        instalar_identificador(conexao)
        obtido = conexao.execute(f"SELECT identificador([{lista}])").fetchone()

    assert obtido is not None
    assert obtido[0] == identificador(*partes)


def test_esquema_das_identidades_e_o_declarado(config_de_identidade: Config) -> None:
    resultado = gerar(config_de_identidade, [socio("11111111", nome="FULANO DE TAL")])

    with duckdb.connect() as conexao:
        descricao = conexao.execute(
            f"DESCRIBE SELECT * FROM read_parquet('{resultado.caminho.as_posix()}')"
        ).fetchall()
    assert tuple(coluna[0] for coluna in descricao) == COLUNAS_IDENTIDADES


def test_sem_socios_a_mensagem_diz_o_que_fazer(tmp_path: Path) -> None:
    config = Config(competencia="2026-06", data_dir=tmp_path, uf_alvo="SP")
    (tmp_path / "silver" / "2026-06").mkdir(parents=True)

    with pytest.raises(SociosAusentesError, match="camada silver"):
        gerar_identidades(config)
