# Dependências vendorizadas

Bibliotecas que moram no repositório em vez de virem de um gerenciador de pacotes
ou de uma CDN. Cada uma traz **origem, versão e SHA-256**, pela mesma razão que o
`docs/layout_rfb.md` registra a soma do PDF da Receita: dependência sem
procedência é dependência que ninguém consegue conferir depois.

## `cytoscape.min.js`

| | |
|---|---|
| versão | **3.34.1** |
| origem | `https://cdn.jsdelivr.net/npm/cytoscape@3.34.1/dist/cytoscape.min.js` |
| tamanho | 435.503 bytes (425 KiB) |
| SHA-256 | `5141892eb19898946e5af8300e14cec15a63a22186a4ca56d76819a91e2a3fe6` |
| licença | MIT |
| onde | `src/grafo_societario/web/static/vendor/cytoscape.min.js` |

**Por que não vem de CDN.** A página é servida pela própria API para haver **uma
origem só** — sem CORS, sem segunda configuração, e um despertar em vez de dois
num plano gratuito que hiberna. Puxar a biblioteca de uma CDN reintroduziria
exatamente a segunda origem que a decisão evita, e com um modo de falha a mais: a
demonstração deixaria de funcionar quando a CDN estivesse fora, por motivo que
não tem nada a ver com este projeto.

**Por que não vem do npm.** Não há etapa de build nesta página, e não há
`package.json`. Acrescentar um ecossistema inteiro para buscar um arquivo seria
ferramenta por ferramenta — a mesma razão pela qual não há vitest.

**O que isso custa.** 425 KiB no artefato de deploy, sobre 416,1 MB de 500. É
0,1% da folga.

**Como conferir.** A soma acima é do arquivo como ele está no repositório:

```bash
python -c "import hashlib,pathlib;print(hashlib.sha256(pathlib.Path('src/grafo_societario/web/static/vendor/cytoscape.min.js').read_bytes()).hexdigest())"
```

**Como atualizar.** Baixar a versão nova, substituir o arquivo, e atualizar
versão, tamanho e soma nesta tabela. Não há automação disso de propósito: trocar
biblioteca de desenho é decisão, e decisão passa por revisão.
