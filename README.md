# Grafo Societário

[![CI](https://github.com/MuriloPedrazzani/grafo-societario/actions/workflows/ci.yml/badge.svg)](https://github.com/MuriloPedrazzani/grafo-societario/actions/workflows/ci.yml)

Caminhos societários entre empresas brasileiras a partir dos dados abertos de CNPJ da Receita Federal.

> **Status:** em desenvolvimento — Fase 6 de 9. Este README é atualizado a cada fase concluída.

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
| Roda em 8 GB de RAM | Nada de cluster; teto de memória **declarado** e transbordo para disco — pico medido de **4,27 GiB**, 53% da restrição |
| Custo zero (free tier) | Sem banco de grafo gerenciado, sem orquestrador dedicado |
| Artefato de deploy ≤ 500 MB | Grafo em arrays CSR lidos via `mmap` — **416,1 MB**, 17% de folga |
| Recorte por UF da matriz | Parametrizável; o padrão é SP |

## Memória

O teto é **escolhido, não descoberto**. O motor roda com `memory_limit` declarado
(`LIMITE_DE_MEMORIA`, padrão `4GB`) e diretório de transbordo próprio: quando a
tabela não cabe, ela vai para disco em vez de a máquina morrer.

A prova é variar o teto e remedir — o pico acompanha o que foi declarado:

| `LIMITE_DE_MEMORIA` | Pico do pipeline | Tempo da Fase 4 |
|---|---:|---:|
| `4GB` (padrão) | **4,27 GiB** | 33,3 s |
| `2GB` | 2,33 GiB | 37,8 s |
| `1GB` | 1,42 GiB | 47,5 s |

Metade da memória custa 13% de tempo; um quarto custa 43%. Em máquina apertada, é
uma linha no `.env`.

**O pico está na construção do grafo, não no bronze** — e isso surpreende. O
bronze faz 1,83 GiB lendo 23,24 GiB de CSV; a Fase 4 faz mais do dobro lendo 650
MiB de silver, **trinta e sete vezes menos entrada**. O que custa não é o volume:
é o hash de 8,7 milhões de vínculos e a junção contra 10,6 milhões de nós, que
precisam de tabela em memória. Ler e escrever linha a linha é barato em qualquer
tamanho.

Há um piso de cerca de 750 MiB que o teto não controla: o cálculo de componentes
conexos roda em NumPy e SciPy, não no motor de ETL, e esses não transbordam.

Os números por etapa estão em [`docs/benchmark.md`](docs/benchmark.md), gerado
pelo próprio código — o pico é amostrado a cada 50 ms enquanto a etapa roda, e não
lido depois que ela termina, que é o erro que faz uma etapa de 4,3 GiB reportar 86
MiB.

## Privacidade

O quadro societário inclui nomes de pessoas físicas.

**Já construído e verificável hoje:**

**A pessoa física é pseudonimizada, e a decisão acontece na geração.** Com `EXPOR_PF` desligada — o padrão, e o modo em que a instância pública roda — o nome de pessoa física e de sócio estrangeiro **não entra no artefato**. Não é filtro de resposta: os artefatos vão para GitHub Release e para imagem Docker, e nome que entrou no arquivo já saiu. Quem precisa dos nomes reais executa o pipeline localmente com os dados originais, que é para isso que o código é aberto.

Na resposta, essas pessoas recebem um **rótulo local** (`Sócio 2`) que é função da **posição na resposta e de mais nada**. A mesma pessoa, alcançada por outro par de empresas, recebe outro rótulo — então ele não serve para correlacionar consultas nem para remontar quem é.

**A prova é um teste que serve um artefato construído *com* os nomes por uma API configurada *sem* eles**, e exige que a resposta saia limpa. Testar pseudonimização contra um artefato que já não tem nome provaria apenas que não se pode devolver o que não existe — a proteção inteira poderia ser removida e a suíte continuaria verde. Antes dele há um controle positivo exigindo que o artefato realmente guarde os nomes, senão o teste passa por vacuidade.

**Um identificador de pessoa física foi criado e depois removido do artefato.** Ele era `sha256("pessoa_fisica|" + nome + "|" + cpf_mascarado)`, e as duas entradas são públicas: o `Socios` da Receita traz as duas. Enumerar o domínio inteiro e comparar hashes é barato — não é inversão criptográfica, é enumeração. Sal não resolve: publicado, o atacante tem; secreto, quebra a reprodutibilidade do artefato. É a mesma regra que já havia derrubado a máscara de CPF — **chave de junção de volta à fonte** —, aplicada desta vez contra o próprio desenho. A remoção tirou 42,8 MB do artefato de quebra.

E na camada de transformação:

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

## Seis graus de separação não valem aqui

A intuição é conhecida: duas pessoas quaisquer estariam a seis conhecidos de distância. Ela vem de **rede densa**, e o grafo societário não é uma. Medido sobre 60.000 pares aleatórios dentro do maior componente:

| | saltos |
|---|---:|
| mediana | **20** |
| p95 | 32 |
| p99 | 38 |
| máximo observado | **57** |

**Apenas 0,55% dos pares estão a até seis saltos.** A até dez, 2,50%.

A consequência é de produto, não de curiosidade. Um limite de profundidade herdado da intuição responderia *"não procurei tão fundo"* a **99,45%** das consultas que chegam a percorrer o grafo — e responderia isso com aparência de resposta. Por isso a profundidade máxima é argumento obrigatório na busca, sem valor padrão: um número errado ali não falha alto.

### O retrato honesto: a maior parte do recorte é uma empresa e um sócio

Estatística de grafo sem denominador rotulado engana, então aqui vão os dois:

| | valor | sobre o quê |
|---|---:|---|
| grau médio | **1,63** | o grafo inteiro, 10.658.250 nós |
| grau médio | **2,79** | só o maior componente, 1.343.694 nós |
| grau mediano | **1** | o grafo inteiro |
| nós com grau exatamente 1 | **6.487.439 — 60,9%** | o grafo inteiro |

Os dois graus médios estão certos; o que muda é o conjunto. Citar 2,79 como "o grau médio do grafo" seria falso, e citar 1,63 como característica do componente gigante também.

**60,9% dos nós são folha.** Somando aos 2,84 milhões de componentes com mediana de tamanho 3, o retrato é este: a maior parte do recorte são empresas pequenas, com um ou dois sócios, ligadas a mais nada. O componente gigante é interessante justamente por ser **minoria** — 12,61% dos nós —, e não por ser o caso típico.

---

## A API

Três rotas, todas sobre artefatos pré-computados e imutáveis, lidos com `mmap`. Nenhuma delas carrega DuckDB, SciPy ou leitor de Parquet — há teste em processo limpo exigindo isso.

| rota | responde |
|---|---|
| `GET /caminho?de=&para=` | o caminho societário mais curto entre duas empresas, ou por que não há um |
| `GET /vizinhanca?cnpj=` | o subgrafo **induzido** em volta de uma empresa, com todas as arestas entre os nós devolvidos |
| `GET /empresa/{cnpj}` | os atributos e as contagens da própria empresa, **sem** os vizinhos |

`/empresa` não devolve vizinhos de propósito. Não é recorte diferente do mesmo domínio: as 14,8 milhões de empresas sem vínculo **não são nós do grafo**, e `/vizinhanca` não teria o que dizer sobre elas. Rotas com domínios diferentes não são a mesma rota com outro nome.

### Os cinco desfechos, e o que cada um afirma

Toda consulta de caminho responde `200` com um campo `desfecho`. **Só dois deles autorizam dizer que não há vínculo**, e a resposta traz um booleano `afirma_ausencia` para o consumidor não precisar saber quais de cabeça:

| desfecho | significa | afirma ausência? |
|---|---|---|
| `encontrado` | achou o caminho, e ele cabe no limite pedido | — |
| `sem_vinculo` | a empresa existe no recorte e não tem vínculo nenhum | **sim** |
| `componentes_diferentes` | as duas têm vínculos e não se alcançam por caminho nenhum | **sim** |
| `alem_do_limite` | há caminho, **a esta distância**, mais longo que o pedido | não |
| `orcamento_excedido` | há caminho, e a busca desistiu antes de achá-lo | não |

Colapsar qualquer um deles em "não encontrado" faria o serviço afirmar que duas empresas não têm vínculo quando a verdade é outra. A conversão dos desfechos da travessia é um `match` com `assert_never`: um desfecho novo **não compila** até ser tratado aqui.

`404` fica reservado a CNPJ que não é nó nem está no recorte, e `422` a CNPJ malformado. Exigimos os quatorze dígitos com verificador — o `cnpj_basico` de oito não tem dígito de controle, e aceitá-lo faria um erro de digitação virar consulta silenciosa a outra empresa.

### Latência medida

Contra o artefato real de 2026-06, com 10.658.250 nós:

| | mediana | p95 | máximo |
|---|---:|---:|---:|
| `/caminho`, exemplos curtos | **0,02 ms** | 0,87 ms | 0,94 ms |
| `/caminho`, pares aleatórios do maior componente, ponta a ponta | **8,01 ms** | 39,74 ms | 74,03 ms |
| `/vizinhanca`, empresa aleatória | **2,70 ms** | 12,91 ms | 33,67 ms |
| `/vizinhanca`, empresa de grau alto | 24,03 ms | 274,91 ms | **318,47 ms** |

Para caminho curto, **o framework custa mais que o grafo**: a travessia leva 0,78 ms dentro de uma resposta HTTP de poucos milissegundos. Numa vizinhança grande o custo é outro — 96% dele é descompressão do nome de cada nó, a 0,35 ms cada, e não a busca.

### Os três padrões, e por que estes números

**`profundidade_maxima = 10`** — é decisão de produto, não de custo. A distância mediana dentro do maior componente é de **20 saltos**, com p95 de 32 e máximo observado de 57. A mediana foi recusada porque um caminho de 20 saltos atravessa dez empresas intermediárias, e chamar aquilo de vínculo societário afirma mais do que o dado sustenta. A assimetria dos erros fecha a escolha: errar para baixo devolve `alem_do_limite` **com a distância real** — "há caminho, com 22 saltos" —, que é informação verdadeira; errar para cima entregaria trinta saltos com cara de descoberta, e essa perda não se desfaz, porque o leitor já leu.

O limite governa **até onde o caminho é mostrado**, e não até onde a busca vai: ela corre até o fim com o orçamento de visitados como único freio.

**`teto_de_nos = 1.000`** — é orçamento de **latência**, não de bytes. Uma resposta de 3.729 nós ocupa 628 KB, que é tranquilo, e custa 1,4 s. A bola tem dois regimes separados por três ordens de grandeza: de uma empresa aleatória, a mediana a 2 saltos é de **3 nós**; de um dos maiores hubs, o primeiro salto já tem **1.132**. Nenhum valor serve aos dois, e o padrão não tenta: 28 de 30 empresas de grau alto têm um nível recusado, e a resposta diz em `nivel_recusado` de que tamanho ele era. **Falha rápida com informação vence acerto lento.**

**`saltos = 2`** — empresa, sócio, e as outras empresas do sócio. É a unidade que significa alguma coisa, e o primeiro valor em que o subgrafo induzido pode mostrar **ciclo** — que é o achado que uma árvore de busca esconderia.

### Limite de taxa

**60 requisições por minuto por cliente.** O propósito escolheu o número: o limite é contra varredura, não contra pico de visitante — se o link circular e cinquenta pessoas clicarem, travá-las mata a demonstração. A 60 por minuto, varrer os 19.770.618 CNPJs do recorte leva **229 dias** ininterruptos, enquanto um por segundo sustentado está acima do que uma pessoa navegando alcança.

Quem quer o recorte inteiro não deveria varrer: o artefato é publicado em Release e traz mais do que a rota devolve. O `429` diz isso, com `Retry-After` junto.

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
   [ grafo ]     arestas → arrays CSR (.npy) + componentes conexos        ✔ pronto
        │
        ▼
   [ busca ]     caminho societário, vizinhança de k saltos, métricas     ✔ pronto
        │
        ▼
   [ API ]       FastAPI sobre artefatos imutáveis, lidos com mmap        ✔ pronto
        │
        ▼
   [ web ]       página de consulta e desenho do subgrafo                 Fase 7
```

As decisões estão registradas hoje nos módulos que as implementam e nas mensagens de commit, que explicam o porquê de cada uma. Os ADRs formais, em `docs/adr/`, são escritos na Fase 8 — inclusive o da recusa de usar o CPF sem máscara, com o custo medido.

## Stack

Em uso: `Python` · `DuckDB` · `Parquet` · `NumPy/SciPy` · `FastAPI` · `GitHub Actions`
Previsto: `Cytoscape.js` (Fase 7) · `Docker` (Fase 8)

---

## Reprodução

Aquisição, bronze, silver, grafo, busca e API já funcionam ponta a ponta. A página web chega na Fase 7.

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

Cerca de **21%** do tamanho original, com pico de **1,83 GiB nesta etapa** — que
não é o pico do pipeline. A etapa mais pesada não é esta, e sim a construção do
grafo; ver [Memória](#memória). A contagem de registros é conferida antes e depois
de cada conversão, e divergência interrompe o processo.

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

A camada do grafo transforma esses vínculos em arrays CSR lidos por `mmap`, mais
os componentes conexos. Em **33,3 s** sobre a competência 2026-06:

| Artefato | Tamanho | O que é |
|---|---:|---|
| `nos.parquet` | 182,8 MiB | 10.658.250 nós com vínculo, e o que cada um é |
| `existencia.npy` | 75,4 MiB | o recorte inteiro, para responder existência |
| `indptr.npy` + `indices.npy` | 107,0 MiB | a topologia: 8.689.882 arestas |
| `qualificacoes.npy` | 16,6 MiB | o papel de cada vínculo, paralelo a `indices` |
| `componentes.npy` | 40,7 MiB | 2.841.365 componentes conexos |
| **total** | **443 MB** | 11% abaixo do teto de deploy |

### O grafo não tem um gigante que engole tudo

O maior componente tem **1.343.694 nós — 12,61%** do grafo, e o segundo tem 3.731:
trezentas e sessenta vezes menor. A mediana de tamanho é **3**, o agrupamento
típico sendo uma empresa e dois sócios.

Isso decide o desenho da busca. Para um par tirado ao acaso, a resposta "não há
caminho" sai de comparar dois inteiros, sem percorrer nada — 87% dos nós estão
fora do gigante. E quando os dois estão dentro dele, o espaço a percorrer é 1,34
milhão de nós com **grau médio 2,79**, não 10,6 milhões.

O nó de maior grau do recorte — 3.728 vizinhos — **não está no gigante**: ele é o
centro de uma estrela quase pura que forma sozinha o segundo componente. Dentro do
gigante, o maior grau é 3.154.

### O que a construção precisou decidir

**654 pares mútuos**: A é sócia de B e B é sócia de A. Simetrizar sem colapsar o
par não ordenado poria o mesmo vizinho duas vezes na mesma linha, e nada
reclamaria — `indptr` fecharia, o total bateria, e só o grau desses nós viria
inflado.

**9.049 laços** — empresa sócia de si mesma — são descartados e contados. Trinta
nós tinham como único vínculo um laço, e ficam no CSR com grau zero: eles são nós
porque tinham vínculo, e não têm vizinho porque o vínculo que tinham não leva a
lugar nenhum.

**`mmap` economizou partida, não memória.** O CSR inteiro são 123,5 MiB e caberia
na memória com folga. Abrir custa 0,07 MiB e nenhuma desserialização; cem mil
acessos aleatórios trazem 110 MiB. O ganho é não pagar a leitura na partida, e
compartilhar as mesmas páginas entre processos.

**A topologia é conferida contra o SciPy.** O mesmo grafo é montado com
`coo_matrix(...).tocsr()` e comparado array a array nas 17.379.764 posições; os
componentes são reconferidos por um union-find que parte da lista de arestas, e
não do CSR. Duas implementações independentes que partem de lados diferentes da
cadeia só concordam por coincidência se ambas estiverem certas.

Os artefatos são **determinísticos**: duas execuções sobre o mesmo dado produzem
os mesmos bytes, conferidos por SHA-256.

### A busca sobre o grafo

Caminho societário por busca bidirecional em largura, vizinhança de k saltos, e
métricas de rede. Medido na competência 2026-06:

| consulta | tempo |
|---|---:|
| par aleatório do grafo | **0,26 ms** — 98,3% saem do rótulo de componente, sem percorrer nada |
| par dentro do maior componente | **5,6 ms** na mediana, 110 ms no pior caso de 6.500 |
| vizinhança de 3 saltos, teto de 500 nós | 0,23 ms |

**A resposta negativa é a mais barata e a mais comum.** Dois nós em componentes
diferentes não podem ter caminho, e isso se responde comparando dois inteiros.
Percorrer o grafo até esgotar para descobrir que não há caminho seria o
desperdício mais caro possível — e é o caso mais frequente.

**Quatro desfechos, e só um afirma ausência.** `COMPONENTES_DIFERENTES` diz que
não há caminho; `ALEM_DO_LIMITE` e `ORCAMENTO_EXCEDIDO` dizem que **há**, e que
esta busca não o entregou. Colapsar os três em "não encontrado" faria o serviço
afirmar que duas empresas não têm vínculo quando a verdade é "não procurei até lá"
ou "desisti no meio".

**A poda por grau foi medida e recusada.** Ela removeria arestas que existem — um
caminho legítimo pelo contador de três mil empresas deixaria de ser encontrado. E
a medição derrubou a premissa que a motivava: partir do maior hub custa 107 ms e
partir de um nó qualquer custa 109 ms. O limite que entrou no lugar é orçamento de
nós visitados, com desfecho próprio, e a justificativa dele é a cobertura da
amostra e não o custo observado.

**A vizinhança devolve o subgrafo induzido, não a árvore de busca.** Em 65% das
consultas de 3 saltos há aresta entre nós do mesmo nível — o ciclo que a árvore
esconderia, e que é justamente o achado de quem investiga: duas empresas ligadas
por um segundo sócio em comum. O corte por teto acontece por nível inteiro, e a
resposta diz quantos nós tinha o nível recusado.

**As métricas são derivadas, não gravadas.** Grau sai de `indptr`, tamanho de
componente sai de um `bincount`, ranking de hubs é ordenação sobre grau derivado.
Zero byte acrescentado ao artefato, e há teste que confere isso comparando o
diretório antes e depois.

A configuração vive em variáveis de ambiente — veja [`.env.example`](.env.example).
`COMPETENCIA` é a única obrigatória.

> O passo a passo completo, do clone ao deploy, com o tempo esperado de cada
> etapa, é publicado na Fase 8.

## Fonte de dados

Receita Federal — Dados Abertos do CNPJ (Empresas, Estabelecimentos, Sócios e tabelas de decodificação). Atualização mensal.

## Licença

MIT
