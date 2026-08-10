# Grafo Societário

[![CI](https://github.com/MuriloPedrazzani/grafo-societario/actions/workflows/ci.yml/badge.svg)](https://github.com/MuriloPedrazzani/grafo-societario/actions/workflows/ci.yml)

Caminhos societários entre empresas brasileiras a partir dos dados abertos de CNPJ da Receita Federal.

> **Status:** em desenvolvimento — Fase 0 de 9. Este README é atualizado a cada fase concluída.

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

> Documentação completa de execução será publicada ao final da Fase 1.

```bash
make ingest   # baixa e extrai os dados da Receita Federal
make build    # bronze → silver → grafo CSR
make serve    # sobe a API local
make test     # suíte de testes
```

## Fonte de dados

Receita Federal — Dados Abertos do CNPJ (Empresas, Estabelecimentos, Sócios e tabelas de decodificação). Atualização mensal.

## Licença

MIT
