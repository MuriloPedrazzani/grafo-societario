// A página não reimplementa a semântica da API — ela renderiza a que veio.
//
// `desfecho`, `afirma_ausencia` e `explicacao` são decididos e testados no
// servidor. Se este arquivo reescrevesse qualquer um deles, passaria a existir
// uma segunda versão da mesma decisão, e as duas divergiriam no commit em que
// ninguém olha. O que mora aqui é só a forma: título, tom e ação.
//
// Por isso o mapa abaixo é indexado pelos valores de `DesfechoDaConsulta`, e há
// um teste em Python que lê este arquivo e exige que os cinco apareçam. É feio,
// é comparação de string, e é a única coisa entre "a API ganhou um desfecho" e
// "a página renderiza em branco para ele".

"use strict";

// Os exemplos, e a inversão que eles carregam.
//
// Quase toda demonstração esconde os estados que não são sucesso. Aqui eles são
// **o achado**, e por isso têm botão próprio: quem clicar nos quatro últimos
// entende a tese do projeto sem ler uma linha de README — que a maioria das
// empresas não tem sócio, que a maioria dos pares não se alcança, que quando se
// alcança não são seis graus, e que existe estrutura que não cabe numa tela.
//
// Isso também resolve o problema que abriu a fase: a demonstração deixa de
// depender de o visitante ter sorte na consulta que digitou.
//
// O rótulo diz **o que o exemplo demonstra**, e nunca "exemplo 1": ninguém clica
// num índice, e o rótulo ensina o vocabulário do projeto de graça.
//
// Cada entrada guarda pergunta, nunca resposta. Congelar o resultado faria o
// exemplo testar a si mesmo, e a página continuaria bonita com a API quebrada.
const EXEMPLOS = [
  {
    rotulo: "Três lojas de roupa em cadeia — 4 saltos",
    modo: "caminho",
    de: "21.278.675/0001-77",
    para: "11.844.766/0001-79",
    profundidade: 10,
  },
  {
    rotulo: "Vizinhança com ciclo — 10 nós, 3 ligações além da árvore",
    modo: "vizinhanca",
    de: "16.704.635/0001-00",
    profundidade: 2,
  },
  {
    rotulo: "Empresa sem sócio registrado — o caso de 74,8% do recorte",
    modo: "caminho",
    de: "31.710.836/0001-03",
    para: "21.278.675/0001-77",
    profundidade: 10,
  },
  {
    rotulo: "Duas empresas sem caminho entre elas — o caso de 98,41% dos pares",
    modo: "caminho",
    de: "43.378.083/0001-60",
    para: "04.212.321/0001-00",
    profundidade: 10,
  },
  {
    rotulo: "Vínculo existe, a 22 saltos — por que não são seis graus",
    modo: "caminho",
    de: "19.968.792/0001-10",
    para: "06.057.252/0001-33",
    profundidade: 10,
  },
  {
    rotulo: "Empresa grande demais para desenhar — 3.154 vizinhos",
    modo: "vizinhanca",
    de: "04.770.650/0001-77",
    profundidade: 2,
  },
];

// A página abre com o primeiro já consultado — formulário vazio é página morta.
const EXEMPLO = EXEMPLOS[0];

const DISTANCIA_MEDIANA = 20;
const DEMORA_PARA_AVISAR = 2000;
const SALTOS_DA_VIZINHANCA = 2;

// O teto desta página, e não o da API.
//
// A API devolve até 1.000 nós porque o limite dela é de latência: cada nome custa
// uma descompressão de 0,35 ms. O limite de um **desenho** é outro — 747 nós são
// 119 KB de resposta e uma bola de pelo na tela. A tabela dos dois regimes está
// na descrição do parâmetro justamente para quem desenha escolher o próprio.
//
// A 150, a empresa aleatória nunca encosta no teto (mediana de 3 nós, p95 de 17)
// e o hub é recusado — e a recusa é o achado, não a falha.
const TETO_DA_PAGINA = 150;

// Título e tom por desfecho. O texto explicativo vem da API, nunca daqui.
//
// `tom` separa as duas linguagens visuais da tela: `achado` e `ausencia` são
// RESPOSTA e usam o mesmo cartão do sucesso; `incerto` é o único "não sei".
// Nenhum deles é erro — no dado real, **98,41%** dos pares de nós caem em
// componentes diferentes e 74,8% das empresas não têm sócio. Se a tela só ficasse
// bonita ao achar caminho curto, ficaria feia quase sempre.
const DESFECHOS = {
  encontrado: {
    tom: "achado",
    titulo: (c) => `${saltos(c.distancia)} entre as duas empresas`,
  },
  alem_do_limite: {
    tom: "achado",
    titulo: (c) => `Existe vínculo — a ${saltos(c.distancia)}`,
    contexto: () => `A distância mediana neste grafo é de ${DISTANCIA_MEDIANA} saltos.`,
    acao: (c) => ({
      rotulo: `Procurar o trajeto (${c.distancia} saltos)`,
      profundidade: c.distancia,
    }),
  },
  sem_vinculo: {
    tom: "ausencia",
    titulo: () => "Sem vínculo societário registrado",
  },
  componentes_diferentes: {
    tom: "ausencia",
    titulo: () => "Não há caminho entre as duas",
  },
  orcamento_excedido: {
    tom: "incerto",
    titulo: () => "A busca parou antes de encontrar",
  },
};

// Só `alem_do_limite` tem contexto próprio, e o motivo apareceu ao olhar a tela
// montada: a `explicacao` da API já diz os 74,8% do `sem_vinculo` e o "definitiva"
// do `componentes_diferentes`, palavra por palavra. Repetir aqui era a segunda
// versão da mesma frase — a que fica velha quando a primeira muda. O que a API
// não diz é a distância mediana, que é o que transforma "20 saltos" em escala.

const alvo = (id) => document.getElementById(id);
const resultado = alvo("resultado");
let modo = "caminho";
let desenhoAtual = null;

function saltos(quantos) {
  if (quantos === null || quantos === undefined) return "distância desconhecida";
  return quantos === 1 ? "1 salto" : `${quantos} saltos`;
}

function contar(quantos, singular, plural) {
  return quantos === 1 ? `1 ${singular}` : `${quantos} ${plural}`;
}

// A API marca ênfase com `**` porque o mesmo texto aparece no /docs, que é
// Markdown. Aqui ela vira <strong> montando nós de texto — **nunca innerHTML**,
// que transformaria texto vindo do servidor em HTML executável.
function comEnfase(texto) {
  const fragmento = document.createDocumentFragment();
  (texto || "").split("**").forEach((parte, indice) => {
    if (!parte) return;
    fragmento.appendChild(
      indice % 2 ? elemento("strong", null, parte) : document.createTextNode(parte)
    );
  });
  return fragmento;
}

function elemento(tag, classe, texto) {
  const no = document.createElement(tag);
  if (classe) no.className = classe;
  if (texto !== undefined) no.textContent = texto;
  return no;
}

function somenteDigitos(texto) {
  return (texto || "").replace(/[^0-9]/g, "");
}

// A forma é conferida aqui; o dígito verificador é conferido no servidor.
//
// Repetir o cálculo do verificador em JavaScript criaria uma segunda
// implementação do mesmo algoritmo, num arquivo sem teste de regressão — e duas
// implementações divergem no commit em que ninguém olha. O erro comum (número
// de dígitos) é pego de graça; o verificador volta em 422 com a mensagem do
// servidor, que é testada.
function conferirForma(valor) {
  const digitos = somenteDigitos(valor);
  if (digitos.length === 0) return "Informe o CNPJ.";
  if (digitos.length === 8) {
    return "São os quatorze dígitos, e não os oito do CNPJ básico: sem o verificador, " +
      "um erro de digitação vira consulta a outra empresa.";
  }
  if (digitos.length !== 14) return `São quatorze dígitos; vieram ${digitos.length}.`;
  return null;
}

function mostrarErroDeCampo(campo, mensagem) {
  const aviso = alvo(`erro-${campo}`);
  aviso.textContent = mensagem || "";
  aviso.hidden = !mensagem;
  alvo(campo).setAttribute("aria-invalid", mensagem ? "true" : "false");
}

// ----------------------------------------------------------------- cartões

function cartao(tom, selo, titulo, explicacao) {
  const caixa = elemento("article", `cartao cartao-${tom}`);
  caixa.appendChild(elemento("p", "selo", selo));
  caixa.appendChild(elemento("h2", "titulo", titulo));
  const paragrafo = elemento("p", "explicacao");
  paragrafo.appendChild(comEnfase(explicacao));
  caixa.appendChild(paragrafo);
  return caixa;
}

function cartaoDoCaminho(corpo) {
  const forma = DESFECHOS[corpo.desfecho];
  // Desfecho que a página não conhece ainda assim se explica: `explicacao` vem
  // preenchida sempre. Degradar para o texto do servidor é melhor que uma tela
  // em branco — mas o teste de fronteira existe para isto não acontecer.
  const caixa = cartao(
    forma ? forma.tom : "incerto",
    corpo.desfecho,
    forma ? forma.titulo(corpo) : corpo.desfecho,
    corpo.explicacao
  );

  if (forma && forma.contexto) {
    caixa.appendChild(elemento("p", "contexto", forma.contexto(corpo)));
  }
  if (forma && forma.acao) {
    const acao = forma.acao(corpo);
    const botao = elemento("button", "acao", acao.rotulo);
    botao.type = "button";
    botao.addEventListener("click", () => {
      alvo("profundidade").value = acao.profundidade;
      consultar();
    });
    caixa.appendChild(botao);
  }
  return caixa;
}

// A recusa do teto é superfície de desenho, e não erro.
//
// "Esta empresa tem 1.132 vizinhos no primeiro salto" é o achado do hub: é a
// tela que mostra que o grafo tem estrutura que não cabe numa tela. Por isso ela
// usa o mesmo cartão da resposta, com o tom de achado — nunca a faixa de
// problema.
function cartaoDaVizinhanca(corpo) {
  if (!corpo.tem_vinculo) {
    return cartao("ausencia", "sem_vinculo", "Sem vínculo societário registrado", corpo.explicacao);
  }

  const quantos = corpo.nos.length;
  // Arestas além das que uma árvore teria: são os ciclos, e são o motivo de o
  // subgrafo induzido valer mais que a árvore de busca. Duas empresas que
  // compartilham um segundo sócio aparecem ligadas aqui e soltas numa árvore.
  const alemDaArvore = Math.max(0, corpo.arestas.length - (quantos - 1));

  let titulo;
  if (corpo.truncada && corpo.saltos === 0) {
    titulo = `Esta empresa tem ${corpo.nivel_recusado.toLocaleString("pt-BR")} vizinhos ` +
      "no primeiro salto";
  } else if (corpo.truncada) {
    titulo = `${contar(quantos, "nó desenhado", "nós desenhados")}; o salto seguinte ` +
      `traz mais ${corpo.nivel_recusado.toLocaleString("pt-BR")}`;
  } else if (alemDaArvore > 0) {
    // O título diz o que a `explicacao` da API não diz. Repetir "N nós a até X
    // saltos", que é a primeira frase dela, era a segunda versão da mesma frase.
    titulo = `${contar(quantos, "nó", "nós")} e ${contar(quantos === 1 ? 0 : corpo.arestas.length, "vínculo", "vínculos")} — ` +
      `${contar(alemDaArvore, "ligação a mais", "ligações a mais")} que numa árvore`;
  } else {
    titulo = `${contar(quantos, "nó", "nós")}, sem nenhum ciclo`;
  }

  const caixa = cartao("achado", "vizinhanca", titulo, corpo.explicacao);
  if (alemDaArvore > 0) {
    caixa.appendChild(
      elemento(
        "p",
        "contexto",
        "As ligações a mais são o que uma árvore de busca esconderia: elas fecham ciclo, " +
          "e é por isso que o que volta é o subgrafo induzido — toda aresta entre os nós " +
          "devolvidos, inclusive as do mesmo salto."
      )
    );
  }
  if (corpo.truncada) {
    caixa.appendChild(
      elemento(
        "p",
        "contexto",
        `Esta página desenha no máximo ${TETO_DA_PAGINA} nós. O limite de um desenho não ` +
          "é o de uma resposta: a API devolveria até mil, e mil nós numa tela não se leem. " +
          "O nível foi recusado inteiro — meio nível pareceria completo sem ser."
      )
    );
  }
  return caixa;
}

// A lista mora **dentro da figura**, e não no cartão.
//
// Ela é a alternativa textual do desenho — mesma informação, para quem lê em vez
// de olhar —, então pertence ao mesmo elemento. E a ordem de leitura passa a ser
// a certa: o que aconteceu, a figura, os detalhes. Antes eram 428 px de lista
// entre o título e o desenho, o que empurrava a figura para fora da tela em
// qualquer janela normal.
function preencherLista(nos) {
  const lista = alvo("lista");
  lista.replaceChildren();
  for (const no of nos) {
    const item = elemento("li", `no no-${no.tipo}`);
    item.appendChild(elemento("span", "nome", no.nome || no.rotulo || "—"));
    item.appendChild(elemento("span", "detalhe", detalheDoNo(no)));
    lista.appendChild(item);
  }
}

function detalheDoNo(no) {
  const partes = [];
  if (no.cnpj) partes.push(no.cnpj);
  partes.push(contar(no.vinculos_no_recorte, "vínculo no recorte", "vínculos no recorte"));
  if (no.profundidade !== undefined) partes.push(`${saltos(no.profundidade)} da origem`);
  if (no.no_recorte === false) partes.push("matriz fora do recorte");
  return partes.join(" · ");
}

// Problema tem outra cara de propósito: faixa, e não cartão. `sem_vinculo` e
// `componentes_diferentes` são resposta e nunca podem parecer isto.
function faixaDeProblema(titulo, texto, codigo) {
  const faixa = elemento("aside", "problema");
  faixa.appendChild(elemento("h2", "titulo", titulo));
  faixa.appendChild(elemento("p", "explicacao", texto));
  if (codigo) faixa.appendChild(elemento("p", "codigo", codigo));
  return faixa;
}

// ----------------------------------------------------------------- desenho

function esconderDesenho() {
  alvo("desenho").hidden = true;
  if (desenhoAtual) {
    desenhoAtual.destroy();
    desenhoAtual = null;
  }
}

function mostrarDesenho(elementos, nos) {
  if (!elementos.length) {
    esconderDesenho();
    return;
  }
  preencherLista(nos);
  alvo("desenho").hidden = false;
  if (desenhoAtual) desenhoAtual.destroy();
  alvo("detalhe-do-no").textContent = "Clique num nó para ver o nome inteiro.";
  desenhoAtual = window.Desenho.desenhar(alvo("tela"), elementos, (no) => {
    alvo("detalhe-do-no").textContent = `${no.nome || no.rotulo} — ${detalheDoNo(no)}`;
  });
}

function limpar() {
  resultado.replaceChildren();
}

function carregando() {
  limpar();
  esconderDesenho();
  const aviso = elemento("p", "carregando", "consultando o grafo…");
  resultado.appendChild(aviso);
  // Num plano gratuito que hiberna, a primeira consulta acorda a instância.
  // Dizer isso é a diferença entre "site quebrado" e "site honesto".
  return setTimeout(() => {
    aviso.textContent =
      "consultando o grafo… a instância é gratuita e pode estar acordando.";
  }, DEMORA_PARA_AVISAR);
}

// ---------------------------------------------------------------- consulta

function enderecoDaConsulta() {
  const de = somenteDigitos(alvo("de").value);
  const profundidade = alvo("profundidade").value || "1";
  if (modo === "vizinhanca") {
    return `/vizinhanca?${new URLSearchParams({
      cnpj: de,
      saltos: profundidade,
      teto_de_nos: String(TETO_DA_PAGINA),
    })}`;
  }
  return `/caminho?${new URLSearchParams({
    de,
    para: somenteDigitos(alvo("para").value),
    profundidade_maxima: profundidade,
  })}`;
}

async function consultar() {
  const problemaDe = conferirForma(alvo("de").value);
  const problemaPara = modo === "caminho" ? conferirForma(alvo("para").value) : null;
  mostrarErroDeCampo("de", problemaDe);
  if (modo === "caminho") mostrarErroDeCampo("para", problemaPara);
  if (problemaDe || problemaPara) {
    limpar();
    esconderDesenho();
    return;
  }

  const relogio = carregando();
  alvo("consultar").disabled = true;
  try {
    const resposta = await fetch(enderecoDaConsulta());
    const corpo = await resposta.json();
    alvo("cru").textContent = JSON.stringify(corpo, null, 2);
    limpar();

    if (!resposta.ok) {
      resultado.appendChild(faixaDoStatus(resposta, corpo));
      esconderDesenho();
      return;
    }
    if (modo === "vizinhanca") {
      resultado.appendChild(cartaoDaVizinhanca(corpo));
      mostrarDesenho(corpo.tem_vinculo ? window.Desenho.elementosDaVizinhanca(corpo) : [], corpo.nos);
    } else {
      resultado.appendChild(cartaoDoCaminho(corpo));
      mostrarDesenho(window.Desenho.elementosDoCaminho(corpo.caminho || []), corpo.caminho || []);
    }
  } catch (erro) {
    limpar();
    esconderDesenho();
    resultado.appendChild(
      faixaDeProblema(
        "Não consegui falar com o serviço",
        "A conexão falhou. Num plano gratuito a instância hiberna, então tentar de " +
          "novo em alguns segundos costuma resolver.",
        String(erro)
      )
    );
  } finally {
    clearTimeout(relogio);
    alvo("consultar").disabled = false;
  }
}

function faixaDoStatus(resposta, corpo) {
  const detalhe = corpo && corpo.detail ? corpo.detail : "";
  if (resposta.status === 404) {
    return faixaDeProblema("Empresa desconhecida", detalhe);
  }
  if (resposta.status === 422) {
    return faixaDeProblema("CNPJ inválido", detalhe || "O CNPJ informado não é válido.");
  }
  if (resposta.status === 429) {
    const espera = resposta.headers.get("Retry-After");
    return faixaDeProblema(
      "Limite de consultas atingido",
      detalhe,
      espera ? `Tente de novo em ${espera} segundos.` : null
    );
  }
  return faixaDeProblema(
    "Erro no serviço",
    detalhe || "A consulta não pôde ser respondida.",
    corpo && corpo.erro_id ? `Identificador da ocorrência: ${corpo.erro_id}` : null
  );
}

// ------------------------------------------------------------------- modos

// Ajustar é só a forma; trocar é ajustar e consultar. A separação existe porque
// um botão de exemplo precisa do ajuste **antes** de preencher os campos, e
// consultar no meio disso dispararia a consulta com o campo antigo.
function ajustarModo(novo) {
  modo = novo;
  const vizinhanca = novo === "vizinhanca";
  alvo("bloco-para").hidden = vizinhanca;
  alvo("rotulo-de").textContent = vizinhanca ? "Empresa" : "Empresa de origem";
  alvo("modo-caminho").setAttribute("aria-selected", String(!vizinhanca));
  alvo("modo-vizinhanca").setAttribute("aria-selected", String(vizinhanca));
  mostrarErroDeCampo("para", null);
}

function trocarModo(novo) {
  ajustarModo(novo);
  alvo("profundidade").value =
    novo === "vizinhanca" ? SALTOS_DA_VIZINHANCA : EXEMPLO.profundidade;
  consultar();
}

// ------------------------------------------------------------------ partida

async function preencherFaixa() {
  try {
    const saude = await (await fetch("/health")).json();
    alvo("faixa").textContent =
      `competência ${saude.competencia} · ${saude.uf_alvo} · ` +
      `${saude.grafo.empresas_no_recorte.toLocaleString("pt-BR")} empresas · ` +
      `${saude.grafo.arestas.toLocaleString("pt-BR")} vínculos societários`;
  } catch {
    alvo("faixa").textContent = "não consegui ler os dados que estão no ar.";
  }
}

function aplicar(exemplo) {
  ajustarModo(exemplo.modo);
  alvo("de").value = exemplo.de;
  if (exemplo.para) alvo("para").value = exemplo.para;
  alvo("profundidade").value = exemplo.profundidade;
  consultar();
}

function montarExemplos() {
  const caixa = alvo("exemplos");
  for (const exemplo of EXEMPLOS) {
    const botao = elemento("button", "exemplo", exemplo.rotulo);
    botao.type = "button";
    botao.addEventListener("click", () => aplicar(exemplo));
    caixa.appendChild(botao);
  }
}

function iniciar() {
  montarExemplos();
  alvo("de").value = EXEMPLO.de;
  alvo("para").value = EXEMPLO.para;
  alvo("profundidade").value = EXEMPLO.profundidade;
  alvo("consulta").addEventListener("submit", (evento) => {
    evento.preventDefault();
    consultar();
  });
  alvo("modo-caminho").addEventListener("click", () => trocarModo("caminho"));
  alvo("modo-vizinhanca").addEventListener("click", () => trocarModo("vizinhanca"));
  // O `/health` é barato e não entra no limite: ele preenche o primeiro segundo
  // enquanto a consulta do exemplo corre.
  preencherFaixa();
  consultar();
}

document.addEventListener("DOMContentLoaded", iniciar);
