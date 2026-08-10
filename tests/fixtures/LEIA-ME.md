# Fixtures — amostras dos arquivos da Receita Federal

Amostras da competência **2026-06**, extraídas dos arquivos reais em 2026-08-10.
Servem para exercitar o parser do bronze contra o formato que a fonte realmente
tem, e não contra o formato que seria conveniente que ela tivesse.

Todas seguem a forma da origem: separador `;`, **todos** os campos entre aspas
duplas (inclusive numéricos e vazios, gravados como `""`), codificação
**latin-1**, terminador **LF**, e nenhuma linha de cabeçalho.

## Por que parte do conteúdo é sintética

Fixture entra no histórico do git e fica lá para sempre, inclusive depois de
removida. Os arquivos da Receita são públicos, mas **carregam dado de pessoa
física** — e este projeto promete, no README e no ADR-006, que pessoa física não
aparece em artefato publicado. Publicar aqui o que a API esconde seria contradizer
a própria tese.

A regra aplicada foi **redigir por coluna, nunca excluir por linha**. Excluir as
linhas problemáticas distorceria a distribuição do arquivo — em Empresas, por
exemplo, restariam 22% dos registros e sumiria justamente o caso mais comum.
Substituir o conteúdo de uma coluna preserva a proporção e a forma de todo o resto.

| Arquivo | Conteúdo | O que foi redigido |
|---|---|---|
| `Empresas0.csv` | Real, com uma coluna redigida | `razao_social` em 161 de 200 registros |
| `Estabelecimentos0.csv` | Real, com sete colunas esvaziadas | os 7 campos de contato, em todos os registros |
| `Socios0.csv` | Estrutura real, conteúdo sintético | nome, documento, representante legal e nome do representante |
| Tabelas de domínio | Reais e intactas | nada — são códigos e descrições, sem pessoa |

### `Empresas0.csv`

78% dos registros da fonte têm natureza jurídica `2135` (empresário individual), e
nesses a `razao_social` é literalmente **nome completo seguido do CPF sem máscara**
— pior que o arquivo de Socios, onde o CPF ao menos vem mascarado.

A coluna foi substituída onde a natureza é `2135` ou onde havia oito ou mais
dígitos seguidos: 161 dos 200 registros, proporção que espelha a do arquivo real.
Os nomes são sintéticos e os onze dígitos são **reprovados de propósito no cálculo
do dígito verificador do CPF**, para nenhum deles poder coincidir com um CPF real.
Todo o resto da linha é o dado original.

### `Estabelecimentos0.csv`

87,8% dos registros da fonte têm e-mail preenchido. Os sete campos de contato
(`ddd_1`, `telefone_1`, `ddd_2`, `telefone_2`, `ddd_fax`, `fax`,
`correio_eletronico`) foram esvaziados em **todos** os registros.

Campo de contato vazio não é invenção: 9,6% dos registros reais já são assim, e a
forma gravada é a mesma que a fonte usa, `""`. As características que o parser
precisa exercitar vivem em `logradouro`, `complemento` e `nome_fantasia`, não em
contato.

Dois registros vieram da partição 8, não da 0, porque a quebra de linha dentro de
campo citado não ocorre na partição 0.

O byte `0x8F` foi reposto em `complemento`. No arquivo real ele aparece 5 vezes em
23,24 GiB, **todas em `correio_eletronico`** — a coluna que esta fixture esvazia.
Sem a reposição, o caminho de decodificação deixaria de ser exercitado.

### `Socios0.csv`

Único arquivo cujo conteúdo é inteiramente sintético. Ele carrega nome de pessoa
física e CPF mascarado em quase todo registro, e não há coluna a preservar.

O que veio do arquivo real e foi mantido intacto: quantidade e ordem dos campos,
códigos de qualificação, datas de entrada, código de país, faixa etária, e a
**forma do documento por tipo de sócio** — `1` pessoa jurídica traz 14 dígitos,
`2` pessoa física traz `***DDDDDD**`, `3` estrangeiro traz campo vazio.

As armadilhas de parser **não ocorrem naturalmente** em Socios, e foram injetadas
nos nomes sintéticos de propósito: um registro com `;` dentro de campo citado, um
com quebra de linha dentro do campo, e um com o byte `0x8F`. Nisso a fixture é
mais dura que o arquivo real.

Isso é deliberado, e vale para todas elas: **fixture testa o código, não documenta
a fonte; a fonte está documentada em [`docs/layout_rfb.md`](../../docs/layout_rfb.md).**
Com os dois papéis separados, uma fixture mais dura que a realidade é a direção
certa de errar — o custo é um teste que exercita um caso que talvez nunca ocorra,
e o custo do inverso é um parser que quebra em produção.

## Armadilhas que estas fixtures existem para pegar

1. **Separador dentro de campo citado.** `QUADRA 39;LOTE 07` em `complemento`
   atinge 4 a 5% dos registros de Estabelecimentos. Dividir por `;` devolve 31
   campos em vez de 30 e corrompe o endereço em silêncio.
2. **Quebra de linha dentro de campo citado.** `Estabelecimentos8.csv` tem
   4.753.438 linhas físicas para 4.753.435 registros. Contar linha física dá
   número errado de registros.
3. **Byte `0x8F`.** Não é atribuído em cp1252 — decodificar como cp1252 levanta
   exceção. É o que prova que a codificação correta é latin-1.
4. **Zero à esquerda em código.** `"00"`, `"01"`, `"08"` convivem com `"2"` e
   `"3"` no mesmo campo, na própria fonte. Comparar como número perde registro.

## Reprodução

As amostras saem de `data/extraido/<competencia>/` depois de
`grafo-societario ingest`. Os registros das tabelas de domínio e os primeiros 200
de cada arquivo grande são determinísticos; a substituição de conteúdo usa semente
fixa (`20260810`).
