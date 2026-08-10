# Grafo Societário

[![CI](https://github.com/MuriloPedrazzani/grafo-societario/actions/workflows/ci.yml/badge.svg)](https://github.com/MuriloPedrazzani/grafo-societario/actions/workflows/ci.yml)

Caminhos societários entre empresas brasileiras a partir dos dados abertos de CNPJ da Receita Federal.

> **Status:** em desenvolvimento — Fase 2 de 9. Este README é atualizado a cada fase concluída.

---

## O problema

O quadro societário de todas as empresas brasileiras é dado público. Na prática, é inutilizável: dezenas de arquivos compactados, CSVs de milhões de linhas sem cabeçalho, codificação `latin-1`, chaves compostas e códigos sem legenda embutida.

Responder uma pergunta simples — *"estas duas empresas têm algum vínculo societário?"* — hoje exige consulta manual, CNPJ por CNPJ.

## A proposta

Um pipeline reprodutível que transforma os arquivos brutos da Receita Federal em um grafo consultável, e uma API que responde em milissegundos:

- existe caminho societário entre a empresa A e a empresa B?
- qual é esse caminho?
- quem está a até N saltos de distância de uma empresa?

## Restrições de projeto

Estas restrições são deliberadas e moldam toda a arquitetura:

| Restrição | Implicação |
|---|---|
| Roda em 8 GB de RAM | Nada de cluster; processamento *out-of-core* com DuckDB |
| Custo zero (free tier) | Sem banco de grafo gerenciado, sem orquestrador dedicado |
| Artefato de deploy ≤ 500 MB | Grafo serializado em arrays CSR lidos via `mmap` |
| Recorte por UF da matriz | Parametrizável; o padrão é SP |

## Privacidade

O quadro societário inclui nomes de pessoas físicas. A API pública **pseudonimiza pessoas físicas por padrão** — elas aparecem como identificadores opacos, nunca como nomes. O código é aberto: quem precisar dos nomes reais executa o pipeline localmente com os dados originais.

Este projeto descreve **estrutura de rede**. Não avalia idoneidade, não imputa conduta e não deve ser usado como base para decisão sobre pessoas ou empresas.

---

## Arquitetura

```
Receita Federal (ZIP/CSV)
        │
        ▼
   [ ingestão ]  download com retry, cache e verificação de integridade
        │
        ▼
   [ bronze ]    CSV → Parquet, fiel à origem, tudo como texto
        │
        ▼
   [ silver ]    tipagem, recorte por UF, decodificação, identidade de sócios
        │
        ▼
   [ grafo ]     arestas → arrays CSR (.npy) + componentes conexos
        │
        ▼
   [ API ]       FastAPI sobre artefatos imutáveis, lidos com mmap
```

Decisões arquiteturais e seus trade-offs estão documentados em [`docs/adr/`](docs/adr/).

## Stack

`Python` · `DuckDB` · `Parquet` · `NumPy/SciPy` · `FastAPI` · `Cytoscape.js` · `Docker` · `GitHub Actions`

---

## Reprodução

A aquisição já funciona ponta a ponta. As demais etapas chegam nas fases seguintes.

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

A configuração vive em variáveis de ambiente — veja [`.env.example`](.env.example).
`COMPETENCIA` é a única obrigatória.

> O passo a passo completo, do clone ao deploy, com o tempo esperado de cada
> etapa, é publicado na Fase 8.

## Fonte de dados

Receita Federal — Dados Abertos do CNPJ (Empresas, Estabelecimentos, Sócios e tabelas de decodificação). Atualização mensal.

## Licença

MIT
