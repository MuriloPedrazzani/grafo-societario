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

// Um dos pares curados: varejo de roupas, quatro saltos, com pessoa física no
// meio. A página abre com ele consultado — formulário vazio é página morta.
const EXEMPLO = { de: "21.278.675/0001-77", para: "11.844.766/0001-79", profundidade: 10 };

const DISTANCIA_MEDIANA = 20;
const DEMORA_PARA_AVISAR = 2000;

// Título e tom por desfecho. O texto explicativo vem da API, nunca daqui.
//
// `tom` separa as duas linguagens visuais da tela: `achado` e `ausencia` são
// RESPOSTA e usam o mesmo cartão do sucesso; `incerto` é o único "não sei".
// Nenhum deles é erro — no dado real, 87% dos pares caem em componentes
// diferentes e 74,8% das empresas não têm sócio. Se a tela só ficasse bonita ao
// achar caminho curto, ficaria feia quase sempre.
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

function saltos(quantos) {
  if (quantos === null || quantos === undefined) return "distância desconhecida";
  return quantos === 1 ? "1 salto" : `${quantos} saltos`;
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

// ----------------------------------------------------------------- desenho

function cartaoDeResposta(corpo) {
  const forma = DESFECHOS[corpo.desfecho];
  // Desfecho que a página não conhece ainda assim se explica: `explicacao` vem
  // preenchida sempre. Degradar para o texto do servidor é melhor que uma tela
  // em branco — mas o teste de fronteira existe para isto não acontecer.
  const tom = forma ? forma.tom : "incerto";
  const titulo = forma ? forma.titulo(corpo) : corpo.desfecho;

  const cartao = elemento("article", `cartao cartao-${tom}`);
  cartao.appendChild(elemento("p", "selo", corpo.desfecho));
  cartao.appendChild(elemento("h2", "titulo", titulo));
  const explicacao = elemento("p", "explicacao");
  explicacao.appendChild(comEnfase(corpo.explicacao));
  cartao.appendChild(explicacao);

  if (forma && forma.contexto) {
    cartao.appendChild(elemento("p", "contexto", forma.contexto(corpo)));
  }
  if (forma && forma.acao) {
    const acao = forma.acao(corpo);
    const botao = elemento("button", "acao", acao.rotulo);
    botao.type = "button";
    botao.addEventListener("click", () => {
      alvo("profundidade").value = acao.profundidade;
      consultar();
    });
    cartao.appendChild(botao);
  }
  if (corpo.caminho && corpo.caminho.length) {
    cartao.appendChild(listaDoCaminho(corpo.caminho));
  }
  return cartao;
}

function listaDoCaminho(caminho) {
  const lista = elemento("ol", "caminho");
  for (const no of caminho) {
    const item = elemento("li", `no no-${no.tipo}`);
    item.appendChild(elemento("span", "nome", no.nome || no.rotulo || "—"));
    const detalhe = [];
    if (no.cnpj) detalhe.push(no.cnpj);
    const quantos = no.vinculos_no_recorte;
    detalhe.push(quantos === 1 ? "1 vínculo no recorte" : `${quantos} vínculos no recorte`);
    if (no.no_recorte === false) detalhe.push("matriz fora do recorte");
    item.appendChild(elemento("span", "detalhe", detalhe.join(" · ")));
    lista.appendChild(item);
  }
  return lista;
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

function limpar() {
  resultado.replaceChildren();
}

function carregando() {
  limpar();
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

async function consultar() {
  const de = alvo("de").value;
  const para = alvo("para").value;
  const problemaDe = conferirForma(de);
  const problemaPara = conferirForma(para);
  mostrarErroDeCampo("de", problemaDe);
  mostrarErroDeCampo("para", problemaPara);
  if (problemaDe || problemaPara) {
    limpar();
    return;
  }

  const parametros = new URLSearchParams({
    de: somenteDigitos(de),
    para: somenteDigitos(para),
    profundidade_maxima: alvo("profundidade").value || "10",
  });

  const relogio = carregando();
  alvo("consultar").disabled = true;
  try {
    const resposta = await fetch(`/caminho?${parametros}`);
    const corpo = await resposta.json();
    alvo("cru").textContent = JSON.stringify(corpo, null, 2);
    limpar();
    resultado.appendChild(
      resposta.ok ? cartaoDeResposta(corpo) : faixaDoStatus(resposta, corpo)
    );
  } catch (erro) {
    limpar();
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

function iniciar() {
  alvo("de").value = EXEMPLO.de;
  alvo("para").value = EXEMPLO.para;
  alvo("profundidade").value = EXEMPLO.profundidade;
  alvo("consulta").addEventListener("submit", (evento) => {
    evento.preventDefault();
    consultar();
  });
  // O `/health` é barato e não entra no limite: ele preenche o primeiro segundo
  // enquanto a consulta do exemplo corre.
  preencherFaixa();
  consultar();
}

document.addEventListener("DOMContentLoaded", iniciar);
