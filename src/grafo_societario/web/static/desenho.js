// O desenho, e por que ele não usa layout de força.
//
// ## Posição derivada do dado, e não de semente aleatória
//
// Layout de força começa de posições sorteadas: a mesma consulta desenha
// diferente a cada carregamento. Num projeto que provou determinismo byte a byte
// em cinco artefatos, isso destoaria — e num GIF o desenho tem de ser o que o
// visitante vai ver quando repetir a consulta.
//
// Aqui a posição sai do dado, e por isso significa alguma coisa:
//
// - **Caminho: linha, na ordem dos saltos.** Um caminho *é* uma sequência, e
//   desenhá-lo em teia esconde a única coisa que ele tem a dizer.
// - **Vizinhança: anéis concêntricos por distância.** O anel passa a querer
//   dizer "primeiro salto", "segundo salto", e o desenho carrega informação em
//   vez de só ocupar espaço.
//
// A ordem dentro de cada anel vem da ordem em que a API devolveu os nós, que é
// crescente por índice interno e garantida pela serialização. Mesma consulta,
// mesmos ângulos.

"use strict";

const ESPACO_DO_CAMINHO = 210;
const RAIO_DO_ANEL = 165;

// A folga entre o desenho e a borda da tela. Sai daqui porque o enquadramento
// é feito em dois lugares — no `preset` ao nascer e no reenquadramento abaixo —
// e os dois têm de usar o mesmo número.
const MARGEM_DO_ENQUADRAMENTO = 34;

// Um caminho de 22 saltos são 23 nós: em linha reta dá 4.620 px, e ajustado à
// tela vira uma tira ilegível. A linha quebra, e a quebra é **bustrofédon** — a
// linha ímpar corre ao contrário, então o último nó de uma fica exatamente acima
// do primeiro da seguinte e a aresta entre eles desce em vez de atravessar o
// desenho inteiro. Continua sendo a ordem dos saltos, lida como texto.
const NOS_POR_LINHA = 7;
const ALTURA_DA_LINHA = 130;

// Truncar é obrigatório: razão social passa de sessenta caracteres com
// frequência. O nome inteiro fica a um clique, na legenda abaixo da tela.
const LARGURA_DO_ROTULO = 130;

const ESTILO = [
  {
    selector: "node",
    style: {
      "background-color": "#c9d4e2",
      "border-color": "#5a6270",
      "border-width": 2,
      label: "data(rotulo)",
      "font-size": 11,
      "text-valign": "bottom",
      "text-margin-y": 5,
      "text-max-width": LARGURA_DO_ROTULO,
      "text-wrap": "ellipsis",
      color: "#16181d",
      width: 34,
      height: 34,
    },
  },
  { selector: 'node[tipo = "pessoa_juridica"]', style: { shape: "round-rectangle", width: 46 } },
  {
    selector: 'node[tipo = "pessoa_fisica"]',
    style: { shape: "ellipse", "background-color": "#e8e2f2", "font-style": "italic" },
  },
  {
    selector: 'node[tipo = "estrangeiro"]',
    style: { shape: "diamond", "background-color": "#f2e6d8", "font-style": "italic" },
  },
  // O conector é pessoa jurídica de fora do recorte: ele **é** nó, tem aresta, e
  // só foram ingeridos os vínculos dele com empresas daqui. Desenhado igual aos
  // outros, afirmaria visualmente uma vizinhança completa que não tem — a mesma
  // ressalva que fez a coluna se chamar `vinculos_no_recorte` e não `grau`.
  {
    selector: "node[?conector]",
    style: { "border-style": "dashed", "border-width": 3, "border-color": "#a15c00" },
  },
  {
    selector: "node[?extremo]",
    style: { "border-color": "#1c5d99", "border-width": 4, "font-weight": "bold" },
  },
  {
    selector: "edge",
    style: {
      "curve-style": "straight",
      width: 2,
      "line-color": "#9aa3b0",
      "target-arrow-shape": "none",
    },
  },
  { selector: "edge[?doCaminho]", style: { "line-color": "#1c5d99", width: 3 } },
];

function rotuloDoNo(no) {
  return no.nome || no.rotulo || "—";
}

function posicoesDoCaminho(nos) {
  return nos.map((_, ordem) => {
    const linha = Math.floor(ordem / NOS_POR_LINHA);
    const coluna = ordem % NOS_POR_LINHA;
    const lugar = linha % 2 === 0 ? coluna : NOS_POR_LINHA - 1 - coluna;
    return { x: lugar * ESPACO_DO_CAMINHO, y: linha * ALTURA_DA_LINHA };
  });
}

function posicoesDaVizinhanca(nos) {
  const porAnel = new Map();
  nos.forEach((no, indice) => {
    const anel = porAnel.get(no.profundidade) || [];
    anel.push(indice);
    porAnel.set(no.profundidade, anel);
  });

  const posicoes = new Array(nos.length);
  for (const [profundidade, indices] of porAnel) {
    if (profundidade === 0) {
      indices.forEach((indice) => (posicoes[indice] = { x: 0, y: 0 }));
      continue;
    }
    const raio = profundidade * RAIO_DO_ANEL;
    indices.forEach((indice, ordem) => {
      // O primeiro do anel fica no topo, e os demais em sentido horário. A ordem
      // é a da resposta, então o mesmo pedido produz o mesmo ângulo.
      const angulo = (2 * Math.PI * ordem) / indices.length - Math.PI / 2;
      posicoes[indice] = { x: raio * Math.cos(angulo), y: raio * Math.sin(angulo) };
    });
  }
  return posicoes;
}

function elementosDoCaminho(caminho) {
  const posicoes = posicoesDoCaminho(caminho);
  const nos = caminho.map((no, ordem) => ({
    data: {
      id: String(ordem),
      rotulo: rotuloDoNo(no),
      tipo: no.tipo,
      conector: no.no_recorte === false,
      extremo: ordem === 0 || ordem === caminho.length - 1,
      detalhe: no,
    },
    position: posicoes[ordem],
  }));
  const arestas = caminho.slice(1).map((_, ordem) => ({
    data: {
      id: `a${ordem}`,
      source: String(ordem),
      target: String(ordem + 1),
      doCaminho: true,
    },
  }));
  return [...nos, ...arestas];
}

function elementosDaVizinhanca(corpo) {
  const posicoes = posicoesDaVizinhanca(corpo.nos);
  const nos = corpo.nos.map((no, ordem) => ({
    data: {
      id: String(ordem),
      rotulo: rotuloDoNo(no),
      tipo: no.tipo,
      conector: no.no_recorte === false,
      extremo: no.profundidade === 0,
      detalhe: no,
    },
    position: posicoes[ordem],
  }));
  // As arestas vêm como pares de posições na resposta — o índice denso do grafo
  // nunca sai da API, então a posição é o identificador local do nó.
  const arestas = corpo.arestas.map(([de, para], ordem) => ({
    data: { id: `a${ordem}`, source: String(de), target: String(para) },
  }));
  return [...nos, ...arestas];
}

// O Cytoscape mede o contêiner **uma vez**, no `cytoscape()`, e guarda em
// cache; o `fit` do `preset` calcula zoom e pan a partir dessa medida. Se o
// contêiner não tiver área nesse instante, o enquadramento nasce degenerado e
// **não se recupera sozinho**. Medido, nesta ordem, com a `desenhar` real:
//
//     init com largura 0        zoom 1      largura 0
//     evento `resize`           zoom 1      largura 862   <- autoResize
//     cy.resize()               zoom 1      largura 862
//     cy.fit()                  zoom 0,89   largura 862
//
// O `autoResize` conserta o canvas e **não** refaz o enquadramento: sobra um
// canvas do tamanho certo com o zoom e o pan de um canvas sem área. Sem erro e
// sem aviso, que é a família de falha que este projeto persegue.
//
// **Esta rota não foi reproduzida em navegador de visitante.** Ela apareceu no
// painel de navegador do agente, que não faz layout em aba oculta; o Chrome
// faz, devolve a largura verdadeira, e ali o desenho nasceu enquadrado — em
// duas abas abertas com clique do meio, que é o caminho suspeito. A guarda fica
// como defesa, não como correção de defeito observado.
//
// Os dois eixos entram porque a degeneração tem assinatura diferente em cada
// um: largura 0 dá `zoom 1` com pan (0,0), e altura 0 dá o zoom clampado no
// `minZoom`. Cobrir só a largura deixaria metade do caso de fora.
//
// Arma só quando o nascimento foi degenerado. Reenquadrar a cada mudança de
// tamanho desfaria o zoom e o pan que o visitante escolheu ao redimensionar a
// janela — o desenho não se reorganiza sozinho, pela mesma razão que
// `autoungrabify` está ligado.
function semArea(container) {
  return container.clientWidth === 0 || container.clientHeight === 0;
}

function reenquadrarSeNasceuSemArea(cy, container) {
  if (!semArea(container) || typeof ResizeObserver === "undefined") return;
  const observador = new ResizeObserver(() => {
    if (cy.destroyed() || semArea(container)) return;
    observador.disconnect();
    cy.resize();
    cy.fit(MARGEM_DO_ENQUADRAMENTO);
  });
  observador.observe(container);
  // A consulta seguinte destrói este `cy`; o observador tem de ir junto, senão
  // fica segurando um contêiner que já não é o dele.
  cy.on("destroy", () => observador.disconnect());
}

function desenhar(container, elementos, aoClicarNoNo) {
  const cy = cytoscape({
    container,
    elements: elementos,
    style: ESTILO,
    // `preset` usa as posições que já calculamos. Nenhum layout iterativo roda,
    // então não há semente, não há animação de acomodação e não há variação
    // entre carregamentos.
    layout: { name: "preset", fit: true, padding: MARGEM_DO_ENQUADRAMENTO },
    // O visitante navega; o desenho não se reorganiza sozinho.
    autoungrabify: true,
    minZoom: 0.2,
    maxZoom: 2.5,
  });
  cy.on("tap", "node", (evento) => aoClicarNoNo(evento.target.data("detalhe")));
  reenquadrarSeNasceuSemArea(cy, container);
  return cy;
}

window.Desenho = { desenhar, elementosDoCaminho, elementosDaVizinhanca };
