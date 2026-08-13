# Benchmark da construção do grafo

> Gerado por `grafo_societario.graph.benchmark`. Competência
> **2026-06**, `UF_ALVO=SP`, medido em
> 2026-08-13. Máquina: 20 núcleos lógicos, 16 GiB de RAM, Windows 10, Python 3.11.9.

## Fase 4 — do silver ao grafo consultável

| Etapa | Tempo | Residente na entrada | Pico | Produziu |
|---|---:|---:|---:|---|
| nós com índice denso | 11,6 s | 86 MiB | **4.372 MiB** | 10.658.250 nós · 14.791.390 isoladas |
| arestas sócio-empresa | 4,1 s | 136 MiB | **2.943 MiB** | 8.699.764 vínculos · 8.699.585 pares |
| serialização em CSR | 6,1 s | 558 MiB | **1.490 MiB** | 8.689.882 arestas · 17.379.764 posições |
| componentes conexos | 3,7 s | 137 MiB | **780 MiB** | 2.841.365 componentes · gigante de 1.343.694 |

**Total de 25,5 s**, com pico de memória residente de
**4,27 GiB** — 53,4% do teto de 8 GiB que o projeto promete.

O pico é **amostrado a cada 50 ms enquanto a etapa roda**, e não lido depois que
ela termina. A diferença não é sutil: lida no fim, a primeira etapa reporta uma
residente modesta, porque a essa altura o motor já devolveu o que tinha pegado. O
que ela realmente exigiu da máquina foi **4.372 MiB**.

A coluna de entrada existe porque as quatro etapas rodam no mesmo processo e o
DuckDB não devolve ao sistema tudo o que reserva. O pico absoluto é o que a
promessa de 8 GiB tem de acomodar; a diferença entre as duas colunas é o que cada
etapa custou por si.

**A construção do grafo é o pico de memória do pipeline inteiro**, e por uma
margem que surpreende: o bronze faz 1,83 GiB lendo 23,24 GiB de CSV, e esta fase
faz mais do dobro lendo 650 MiB de silver — **trinta e sete vezes menos entrada**.

O que custa aqui não é o tamanho do dado. É o hash de 8,7 milhões de vínculos e a
junção contra 10,6 milhões de nós, que precisam de tabela em memória e não
transbordam de graça. Ler linha a linha e escrever linha a linha, que é o que o
bronze faz, é barato em qualquer volume.

## Artefatos publicáveis

| Artefato | Bytes | MiB |
|---|---:|---:|
| `nos.parquet` | 146.792.173 | 139,99 |
| `existencia.npy` | 79.082.600 | 75,42 |
| `indptr.npy` | 42.633.132 | 40,66 |
| `indices.npy` | 69.519.184 | 66,30 |
| `qualificacoes.npy` | 17.379.892 | 16,57 |
| `componentes.npy` | 42.633.128 | 40,66 |
| **total** | **398.040.109** | **379,6** |

Contra o teto de 500 MB, sobra
**20,4%**. `arestas.parquet` não entra na
conta: é insumo do CSR, e não é consultado em tempo de resposta.

## Antes da Fase 4

| Etapa | Tempo | Observação |
|---|---:|---|
| download de 36 arquivos (6,79 GiB) | 47,6 min | limitado por rede, 2,4 MiB/s |
| extração e transcodificação | 5,3 min | 23,24 GiB de CSV, ~128 MiB/s |
| bronze (CSV → Parquet) | ~1,7 min | 4,91 GiB, pico de 1,83 GiB |

Medido nas Fases 1 a 3 e não remedido aqui: depende de rede e de 23 GiB de CSV
extraído em disco. **O download é 94% do tempo total do pipeline** — o
processamento inteiro, de CSV bruto a grafo consultável, é uma fração do que se
gasta esperando a Receita Federal entregar os arquivos.

## O que isto custaria num banco de grafo gerenciado

O grafo tem 10.658.250 nós e 8.689.882 arestas.

**No plano gratuito, não cabe — e não é por pouco.** O teto é de
200.000 nós na leitura mais generosa das fontes do
próprio fornecedor, que divergem entre si: a página do produto anuncia 50.000 nós
e 175.000 relacionamentos, e o FAQ, 200.000 e 400.000. Este grafo é **cerca de
53 vezes** o maior desses dois tetos, e mais de duzentas vezes o
menor. Divergência de fonte não muda conclusão quando a distância é de duas ordens
de grandeza — e usar o maior de propósito evita escolher o número que favorece o
argumento.

**No plano pago, o piso é US$ 65/mês**: a menor instância, de
1 GiB, a US$ 65 por GiB/mês. É piso e não estimativa — os
379,6 MiB de artefato deste projeto não
guardam índice nenhum nem propriedade de nó, e um banco de grafo guarda os dois.
São US$ 780/ano no melhor caso, contra **R$ 0**.

Valores verificados em agosto de 2026 e sujeitos a mudança; confira antes de citar.

### A comparação honesta

Não é a mesma coisa, e fingir que é enfraqueceria o argumento. Um banco de grafo
gerenciado entrega linguagem de consulta, transação, escrita e índice sobre
qualquer propriedade. Este projeto entrega **um artefato imutável, somente
leitura, com as consultas decididas de antemão**.

O que se trocou: a capacidade de escrever e de perguntar qualquer coisa. O que se
comprou: custo zero, partida sem desserialização, e um artefato que o sistema
operacional pagina sozinho.

A troca só é boa porque o dado **é** imutável entre competências — a Receita
publica uma vez por mês. Num domínio com escrita concorrente, ela seria péssima, e
a conclusão deste documento se inverteria.
