# Layout dos arquivos da Receita Federal

Referência das colunas dos arquivos de Dados Abertos do CNPJ. Existe para que o
código nunca precise adivinhar nome nem posição de coluna.

## Origem

Fonte primária: **Novo Layout para os DADOS ABERTOS do CNPJ**, publicado pela
Receita Federal em <https://www.gov.br/receitafederal/dados/cnpj-metadados.pdf>.

| | |
|---|---|
| Consultado em | 2026-08-10 |
| Tamanho | 59.315 bytes, 6 páginas |
| SHA-256 | `0a52d6bdfb61a07425e352a0e692932306a5ec9ecad682f5f9e28059e1a5fce0` |

O checksum está aqui para que uma revisão futura do documento seja detectável:
se o hash mudar, este arquivo precisa ser reconferido antes de confiar nele.

O servidor responde **403 a requisições `HEAD`**, mas atende `GET` normalmente —
detalhe que importa para qualquer verificação automatizada de atualização.

## O que este documento afirma, e com que confiança

Nem tudo que o pipeline precisa saber está no PDF. A distinção é deliberada:

| Característica | Procedência | Situação |
|---|---|---|
| Separador `;` | PDF, item 1 | **Confirmado na fonte oficial** |
| Nome e ordem dos campos | PDF, tabelas por arquivo | **Confirmado na fonte oficial** |
| Mascaramento do CPF | PDF, item 2 | **Confirmado na fonte oficial** |
| Códigos de identificador de sócio | PDF, layout Sócios | **Confirmado na fonte oficial** |
| CNAE secundária separada por vírgula | PDF, item 5 | **Confirmado na fonte oficial** |
| Ausência de linha de cabeçalho | Não consta no PDF | A conferir contra o arquivo real |
| Codificação `latin-1` | Não consta no PDF | A conferir contra o arquivo real |
| Decimal com vírgula no capital social | Não consta no PDF | A conferir contra o arquivo real |
| Uso inconsistente de aspas | Não consta no PDF | A conferir contra o arquivo real |

As quatro últimas linhas só saem de "a conferir" na Fase 2, quando as fixtures
com amostra real forem lidas. Até lá, o pipeline não deve tratá-las como certas.

## Formato

O PDF (item 1) especifica apenas: arquivo em formato de carga para banco
relacional, com **ponto e vírgula (`;`) como separador de atributos**.

A camada bronze lê **todas as colunas como texto**, sem conversão. Isso não é
preferência de estilo: em dado público sujo, conversão na ingestão é onde se
perde linha silenciosamente, e a origem deixa de ser reproduzível.

## Empresas — 7 colunas

| # | Nome no projeto | Campo no PDF | Observação |
|---|---|---|---|
| 1 | `cnpj_basico` | CNPJ BÁSICO | Oito primeiros dígitos do CNPJ. Chave de junção entre os três arquivos principais |
| 2 | `razao_social` | RAZÃO SOCIAL / NOME EMPRESARIAL | |
| 3 | `natureza_juridica` | NATUREZA JURÍDICA | Código; decodificar pela tabela de naturezas |
| 4 | `qualificacao_do_responsavel` | QUALIFICAÇÃO DO RESPONSÁVEL | Código; decodificar pela tabela de qualificações |
| 5 | `capital_social` | CAPITAL SOCIAL DA EMPRESA | |
| 6 | `porte` | PORTE DA EMPRESA | `00` não informado, `01` micro, `03` pequeno porte, `05` demais |
| 7 | `ente_federativo_responsavel` | ENTE FEDERATIVO RESPONSÁVEL | Preenchido só para natureza jurídica do grupo `1XXX`; em branco nas demais |

O PDF não define os códigos `02` e `04` de porte. Não invente significado para
eles se aparecerem no dado.

## Estabelecimentos — 30 colunas

O maior volume, e a origem do recorte territorial.

| # | Nome no projeto | Campo no PDF | Observação |
|---|---|---|---|
| 1 | `cnpj_basico` | CNPJ BÁSICO | |
| 2 | `cnpj_ordem` | CNPJ ORDEM | Nono ao décimo segundo dígito |
| 3 | `cnpj_dv` | CNPJ DV | Dois últimos dígitos |
| 4 | `identificador_matriz_filial` | IDENTIFICADOR MATRIZ/FILIAL | `1` matriz, `2` filial. **Base do recorte por UF** |
| 5 | `nome_fantasia` | NOME FANTASIA | |
| 6 | `situacao_cadastral` | SITUAÇÃO CADASTRAL | `01` nula, `2` ativa, `3` suspensa, `4` inapta, `08` baixada |
| 7 | `data_situacao_cadastral` | DATA SITUAÇÃO CADASTRAL | |
| 8 | `motivo_situacao_cadastral` | MOTIVO SITUAÇÃO CADASTRAL | Código |
| 9 | `nome_cidade_exterior` | NOME DA CIDADE NO EXTERIOR | |
| 10 | `pais` | PAIS | Código; decodificar pela tabela de países |
| 11 | `data_inicio_atividade` | DATA DE INÍCIO ATIVIDADE | |
| 12 | `cnae_fiscal_principal` | CNAE FISCAL PRINCIPAL | Código único |
| 13 | `cnae_fiscal_secundaria` | CNAE FISCAL SECUNDÁRIA | **Lista separada por vírgula.** Ver seção de armadilhas |
| 14 | `tipo_logradouro` | TIPO DE LOGRADOURO | |
| 15 | `logradouro` | LOGRADOURO | |
| 16 | `numero` | NÚMERO | Vem `S/N` quando não há número |
| 17 | `complemento` | COMPLEMENTO | |
| 18 | `bairro` | BAIRRO | |
| 19 | `cep` | CEP | |
| 20 | `uf` | UF | Sigla da UF **do estabelecimento**, não da empresa |
| 21 | `municipio` | MUNICÍPIO | Código; decodificar pela tabela de municípios |
| 22 | `ddd_1` | DDD 1 | |
| 23 | `telefone_1` | TELEFONE 1 | |
| 24 | `ddd_2` | DDD 2 | |
| 25 | `telefone_2` | TELEFONE 2 | |
| 26 | `ddd_fax` | DDD DO FAX | |
| 27 | `fax` | FAX | |
| 28 | `correio_eletronico` | CORREIO ELETRÔNICO | E-mail do contribuinte. Dado pessoal; não entra em artefato publicado |
| 29 | `situacao_especial` | SITUAÇÃO ESPECIAL | |
| 30 | `data_situacao_especial` | DATA DA SITUAÇÃO ESPECIAL | |

Note a inconsistência de preenchimento com zero à esquerda **no próprio documento
oficial**: situação cadastral lista `01`, `2`, `3`, `4`, `08`. Comparar esses
códigos como número, ou assumir largura fixa, é caminho para descarte silencioso.
Compare como texto, exatamente como veio.

## Sócios — 11 colunas

É deste arquivo que saem as **arestas do grafo**.

| # | Nome no projeto | Campo no PDF | Observação |
|---|---|---|---|
| 1 | `cnpj_basico` | CNPJ BÁSICO | A empresa. Um dos extremos da aresta |
| 2 | `identificador_socio` | IDENTIFICADOR DE SÓCIO | `1` pessoa jurídica, `2` pessoa física, `3` estrangeiro |
| 3 | `nome_socio_ou_razao_social` | NOME DO SÓCIO (PF) OU RAZÃO SOCIAL (PJ) | |
| 4 | `cnpj_cpf_socio` | CNPJ/CPF DO SÓCIO | **Mascarado quando CPF.** Ausente para estrangeiro |
| 5 | `qualificacao_socio` | QUALIFICAÇÃO DO SÓCIO | Código; atributo da aresta |
| 6 | `data_entrada_sociedade` | DATA DE ENTRADA SOCIEDADE | |
| 7 | `pais` | PAIS | Código do país do sócio estrangeiro |
| 8 | `representante_legal` | REPRESENTANTE LEGAL | CPF do representante, também mascarado |
| 9 | `nome_representante` | NOME DO REPRESENTANTE | |
| 10 | `qualificacao_representante_legal` | QUALIFICAÇÃO DO REPRESENTANTE LEGAL | Código |
| 11 | `faixa_etaria` | FAIXA ETÁRIA | `1` 0–12, `2` 13–20, `3` 21–30, `4` 31–40, `5` 41–50, `6` 51–60, `7` 61–70, `8` 71–80, `9` acima de 80, `0` não se aplica |

## Simples — 7 colunas

Fora do escopo do MVP; documentado para não ser confundido com os demais.

| # | Nome no projeto | Campo no PDF |
|---|---|---|
| 1 | `cnpj_basico` | CNPJ BÁSICO |
| 2 | `opcao_pelo_simples` | OPÇÃO PELO SIMPLES (`S`, `N`, ou em branco para outros) |
| 3 | `data_opcao_simples` | DATA DE OPÇÃO PELO SIMPLES |
| 4 | `data_exclusao_simples` | DATA DE EXCLUSÃO DO SIMPLES |
| 5 | `opcao_pelo_mei` | OPÇÃO PELO MEI (`S`, `N`, ou em branco para outros) |
| 6 | `data_opcao_mei` | DATA DE OPÇÃO PELO MEI |
| 7 | `data_exclusao_mei` | DATA DE EXCLUSÃO DO MEI |

## Tabelas de domínio — 2 colunas cada

Um arquivo por tabela, todas com o mesmo formato `codigo;descricao`:
**países**, **municípios**, **qualificações de sócios**, **naturezas jurídicas**
e **CNAEs**.

| # | Nome no projeto | Campo no PDF |
|---|---|---|
| 1 | `codigo` | CÓDIGO |
| 2 | `descricao` | DESCRIÇÃO |

## Três armadilhas que moldam o projeto

### 1. O CPF vem mascarado, e isso define a identidade de pessoa física

O PDF (item 2) determina que o CNPJ/CPF do sócio e o do representante legal sejam
descaracterizados por "ocultação dos três primeiros dígitos e dos dois dígitos
verificadores", conforme o art. 129 § 2º da Lei nº 13.473/2017.

Um CPF tem 11 dígitos. Ocultados os três primeiros e os dois verificadores,
**sobram seis dígitos visíveis**, na forma `***XXXXXX**`.

Consequência direta: não existe identificador único e confiável de pessoa física
no dado público. Seis dígitos não identificam uma pessoa, e nome sozinho colapsa
homônimos. Por isso a identidade de PF neste projeto é o hash de
(nome normalizado + CPF mascarado) — decisão registrada no ADR-004 — e por isso
a **taxa de colisão residual precisa ser estimada e publicada**, não escondida.

### 2. Sócio estrangeiro não tem documento nenhum

O PDF é explícito: "SÓCIO ESTRANGEIRO NÃO TEM ESTA INFORMAÇÃO". Para
`identificador_socio = 3`, o campo `cnpj_cpf_socio` vem vazio.

Isso significa que o esquema de identidade precisa de três caminhos, não dois:

- `1` pessoa jurídica → identidade é o `cnpj_basico`, exata e sem ambiguidade;
- `2` pessoa física → hash de nome normalizado com CPF mascarado, com colisão residual;
- `3` estrangeiro → **nem documento, nem CPF mascarado**. Restam nome e código do
  país, o que é substancialmente mais frágil que os outros dois casos.

O nó de sócio estrangeiro merece ser marcado como tal na saída, para que ninguém
leia uma fusão desses nós com a mesma confiança de uma fusão por CNPJ.

### 3. Vírgula dentro de arquivo separado por ponto e vírgula

O item 5 do PDF determina que `cnae_fiscal_secundaria`, no layout de
Estabelecimentos, seja preenchido "com cada ocorrência sendo separada por
vírgula, para os casos de várias ocorrências".

O arquivo é separado por `;` e o campo é separado por `,`. São dois níveis de
separação no mesmo arquivo. Duas consequências práticas:

- o bronze **não divide esse campo**: guarda a string inteira, fiel à origem;
- dividir é trabalho da camada silver, e produz relação de um para muitos.

Configurar erradamente o parser de CSV — vírgula como delimitador, ou inferência
automática de separador — despedaça a linha inteira, não só esta coluna.

## O que ainda não está aqui

- **Nomes e URLs dos arquivos ZIP.** A distribuição mudou no fim de janeiro de
  2026 e deixou de ser listagem de diretório. Isso é assunto da aquisição de
  dados, não do layout.
- **Codificação, cabeçalho e uso de aspas**, pelos motivos da tabela de
  procedência: só entram aqui depois de conferidos contra o arquivo real.
