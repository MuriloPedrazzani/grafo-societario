# Grafo Societário

[![CI](https://github.com/MuriloPedrazzani/grafo-societario/actions/workflows/ci.yml/badge.svg)](https://github.com/MuriloPedrazzani/grafo-societario/actions/workflows/ci.yml)

Caminhos societários entre empresas brasileiras a partir dos dados abertos de CNPJ da Receita Federal.

> **Status:** em desenvolvimento — Fase 3 de 9. Este README é atualizado a cada fase concluída.

---

## O problema

O quadro societário de todas as empresas brasileiras é dado público. Na prática, é inutilizável: dezenas de arquivos compactados, CSVs de milhões de linhas sem cabeçalho, codificação `latin-1`, chaves compostas e códigos sem legenda embutida.

Responder uma pergunta simples — *"estas duas empresas têm algum vínculo societário?"* — hoje exige consulta manual, CNPJ por CNPJ.

## A proposta

Um pipeline reprodutível que transforme os arquivos brutos da Receita Federal em um grafo consultável, e uma API capaz de responder em milissegundos:

- existe caminho societário entre a empresa A e a empresa B?
- qual é esse caminho?
- quem está a até N saltos de distância de uma empresa?

## Restrições de projeto

Estas restrições são deliberadas e moldam toda a arquitetura:

| Restrição | Implicação |
|---|---|
| Roda em 8 GB de RAM | Nada de cluster; processamento *out-of-core* com DuckDB — pico medido de 1,83 GiB |
| Custo zero (free tier) | Sem banco de grafo gerenciado, sem orquestrador dedicado |
| Artefato de deploy ≤ 500 MB | Grafo a serializar em arrays CSR lidos via `mmap` (Fase 4) |
| Recorte por UF da matriz | Parametrizável; o padrão é SP |

## Privacidade

O quadro societário inclui nomes de pessoas físicas.

**Compromisso de desenho, ainda não construído (Fase 6):** a API pública pseudonimizará pessoas físicas por padrão — elas aparecerão como identificadores opacos, nunca como nomes. O código é aberto: quem precisar dos nomes reais executa o pipeline localmente com os dados originais.

**Já construído e verificável hoje**, na camada de transformação:

**O CPF sai na transformação, e não dependerá da resposta da API.** A Receita mascara o CPF em toda parte onde teve a chance, mas ele escapa sem máscara dentro da razão social de empresário individual — em **5,2 milhões** de registros só no recorte de São Paulo, 26% do total. Como os artefatos deste projeto são publicados em Release e em imagem Docker, mascarar na resposta não desfaria nada: o dado já teria saído. A supressão acontece na camada que gera os artefatos, e um portão de qualidade varre todos eles antes da publicação, com o varredor validado contra o dado bruto para provar que sabe achar.

**O CPF sem máscara é recusado como identificador.** Ele existe no dado de origem e permitiria identificar o dono de cada empresário individual. Usá-lo seria reidentificação em escala de milhões de pessoas, a partir de uma falha da fonte. O custo dessa recusa está medido: das 19.770.618 empresas do recorte, **14.792.701 não têm nenhum sócio registrado**, porque o dono do empresário individual está dentro do nome da empresa e o projeto decidiu não extraí-lo.

Dessas, **14.791.390 não têm vínculo nenhum** e são nós que caminho societário algum atravessa — 74,8% do recorte. As 1.311 restantes não têm sócio mas **são sócias** de outra empresa, então têm aresta e entram no grafo. A diferença é de 0,009% e não muda a conclusão, mas "sem sócio" e "isolada" são coisas diferentes e o número certo para cada frase é diferente.

**Contato não atravessa.** E-mail, telefone, DDD e fax existem no dado da Receita, ficam na camada local e não entram em nenhum artefato publicado. A ausência é verificada como asserção de esquema, não confiada à intenção.

Este projeto descreve **estrutura de rede**. Não avalia idoneidade, não imputa conduta e não deve ser usado como base para decisão sobre pessoas ou empresas.

## Identidade de pessoa física, e o limite dela

Pessoa jurídica se identifica pelo CNPJ, sem ambiguidade. Pessoa física não: o documento vem mascarado como `***123456**`, com seis dígitos visíveis. A identidade de uma pessoa aqui é o par **nome normalizado + CPF mascarado**.

Nome e seis dígitos não identificam ninguém com certeza. Duas pessoas diferentes recebem a mesma identidade quando coincidem nas duas coisas — e este projeto **mede a frequência disso e publica o número**, em vez de deixá-lo implícito.

> **O que o número significa:** a probabilidade de uma identidade do grafo corresponder a duas pessoas diferentes, e não a uma. Não é taxa de erro do código; é o limite do que o dado disponível permite afirmar.

### Por que a taxa varia por região fiscal

O último dígito visível da máscara é a **região fiscal** do CPF, e o mascaramento da Receita o deixa exposto. Num recorte de São Paulo, **86,65%** dos sócios têm `8` nessa posição — as outras cinco posições são uniformes. O espaço de máscaras, portanto, não é o que aparenta:

| | combinações |
|---|---:|
| espaço nominal (6 dígitos) | 1.000.000 |
| **espaço efetivo medido** | **132.705** |

Sete vezes e meia menor. A consequência é que as 100.000 máscaras da região 8 estão **saturadas** — todas existem, com 48,8 pessoas cada —, enquanto fora dela a máscara quase sempre pertence a uma pessoa só. Uma taxa média esconderia essa diferença inteira:

| CPF de região fiscal | risco de fusão |
|---|---|
| **8** (São Paulo) | **1 em 92.186** |
| qualquer outra | **1 em 1.984.377** |

Vinte vezes de diferença. Por isso a taxa é atributo de cada nó, calculada do dígito daquele CPF, e não um número único do projeto.

No total, estima-se que **cerca de 54** das 5,6 milhões de identidades de pessoa física correspondam a duas pessoas. O método é o índice de Simpson da distribuição empírica de máscaras multiplicado pelo número de pares homônimos, por região. O modelo foi calibrado contra um observável que ele não usa: prevê 586.037 máscaras distintas onde existem 584.902 — erro de **0,19%**.

Sócio estrangeiro é um terceiro caso: não tem documento nenhum, sobra nome e país, e a identidade dele é sinalizada como frágil em vez de estimada. Sócio sem nome não é fundido com ninguém — cada registro vira um nó próprio.

## Todo grau é relativo ao recorte

O pipeline ingere apenas os sócios de empresas cuja **matriz** está na UF alvo. Uma pessoa com participação em 3 empresas em São Paulo e 40 no Rio de Janeiro aparece neste grafo com **3**.

> O número é **piso, nunca total**. Dentro do recorte ele está certo; como afirmação sobre a pessoa, está errado.

Isso vale para grau, para centralidade e para qualquer frase da forma *"fulano participa de N empresas"*. A coluna do artefato chama-se `vinculos_no_recorte`, e não `grau`, exatamente para que a distinção não se perca na leitura.

Vale também para as empresas de fora que entram como conectores: uma holding de outro estado aparece ligada às suas controladas paulistas e invisível em todo o resto do quadro societário dela.

---

## Arquitetura

```
Receita Federal (ZIP/CSV)
        │
        ▼
   [ ingestão ]  download com retry, cache e verificação de integridade   ✔ pronto
        │
        ▼
   [ bronze ]    CSV → Parquet, fiel à origem, tudo como texto            ✔ pronto
        │
        ▼
   [ silver ]    tipagem, recorte por UF, decodificação, identidade       ✔ pronto
        │
        ▼
   [ grafo ]     arestas → arrays CSR (.npy) + componentes conexos        Fase 4
        │
        ▼
   [ API ]       FastAPI sobre artefatos imutáveis, lidos com mmap        Fase 6
```

As decisões estão registradas hoje nos módulos que as implementam e nas mensagens de commit, que explicam o porquê de cada uma. Os ADRs formais, em `docs/adr/`, são escritos na Fase 8 — inclusive o da recusa de usar o CPF sem máscara, com o custo medido.

## Stack

Em uso: `Python` · `DuckDB` · `Parquet` · `GitHub Actions`
Previsto: `NumPy/SciPy` (Fase 4) · `FastAPI` (Fase 6) · `Cytoscape.js` (Fase 7) · `Docker` (Fase 8)

---

## Reprodução

Aquisição, bronze e silver já funcionam ponta a ponta. Grafo e API chegam nas fases seguintes.

```bash
pip install -e ".[dev]"

grafo-societario ingest --competencia 2026-06   # baixa e extrai da Receita Federal
grafo-societario ingest --ultima                # usa a competência mais recente completa
grafo-societario ingest --verificar-integridade # confere o SHA-256 do que está em disco
```

Uma competência ocupa **6,79 GiB comprimidos** e **23,24 GiB** depois de extraída;
o espaço é conferido antes de a extração começar. Rodar de novo não rebaixa nada:
o manifesto registra tamanho, SHA-256, ETag e origem de cada arquivo.

A camada bronze converte esses CSVs em Parquet sem alterar conteúdo — todas as
colunas como texto, nada inferido:

| | CSV | Parquet | Registros |
|---|---:|---:|---:|
| Estabelecimentos | 15,59 GiB | 3,38 GiB | 71.874.448 |
| Empresas | 4,99 GiB | 1,03 GiB | 68.629.148 |
| Sócios | 2,66 GiB | 0,50 GiB | 27.838.448 |
| **Total** | **23,24 GiB** | **4,91 GiB** | **168.342.044** |

Cerca de **21%** do tamanho original, com pico de memória de **1,83 GiB** — dentro
da restrição de 8 GiB do projeto, com folga. A contagem de registros é conferida
antes e depois de cada conversão, e divergência interrompe o processo.

A camada silver recorta pela UF da matriz, tipa, decodifica e pseudonimiza.
Com `UF_ALVO=SP` na competência 2026-06:

| Artefato | Registros | O que é |
|---|---:|---|
| `recorte` | 19.770.618 | empresas cuja matriz está em SP |
| `empresas` | 19.770.618 | tipadas, decodificadas, documento suprimido |
| `socios` | 8.699.764 | vínculos — as arestas do grafo |
| `identidades` | 5.767.316 | nós de sócio, com a confiança de cada um |

Situação cadastral **não filtra**: baixadas são 45,65% do recorte, e vínculo de
empresa que fechou continua sendo vínculo — é o que interessa a quem investiga
sucessão de sócios. Filtrar é decisão de quem consulta, não da transformação.

Dos 267.755 vínculos entre empresas, **19% apontam para fora do recorte**. Eles
são mantidos, e as 36.810 empresas de outras UFs entram como **conectores**:
descartá-las quebraria caminho real, já que duas paulistas podem estar ligadas por
uma holding de outro estado — e responder se esse vínculo existe é o produto.

Cada etapa confere o que produz, e um portão final confere se as quatro tabelas
concordam **entre si**: a cadeia recorte → empresas → sócios → identidades precisa
fechar, toda chave de junção precisa existir do outro lado, e nenhuma coluna de
texto pode conter documento. Regra quebrada interrompe o pipeline; nada é aviso.

Os artefatos são **determinísticos**: duas execuções sobre o mesmo dado produzem
os mesmos bytes, conferidos por SHA-256.

A configuração vive em variáveis de ambiente — veja [`.env.example`](.env.example).
`COMPETENCIA` é a única obrigatória.

> O passo a passo completo, do clone ao deploy, com o tempo esperado de cada
> etapa, é publicado na Fase 8.

## Fonte de dados

Receita Federal — Dados Abertos do CNPJ (Empresas, Estabelecimentos, Sócios e tabelas de decodificação). Atualização mensal.

## Licença

MIT
