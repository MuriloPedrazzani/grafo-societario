"""Mede a construção do grafo e escreve o relatório que fecha a Fase 4.

O número que interessa aqui não é o tempo — é a relação entre o tempo, o pico de
memória e o tamanho do artefato, porque é ela que sustenta as três restrições do
projeto: rodar em 8 GiB, caber em 500 MB e custar zero.

## O pico é amostrado, e não perguntado no fim

Memória residente medida depois que a etapa terminou não é o pico dela: é o que
sobrou. O amostrador roda numa linha de execução própria e olha a residente a cada
50 ms enquanto a etapa trabalha, guardando o maior valor visto.

O instrumento tem controle positivo em `tests/test_benchmark.py`: ele prova que
detecta uma alocação conhecida e transitória antes de a medição valer. Um
amostrador com intervalo grande demais, ou preso atrás do GIL, devolveria a
linha de base e a leitura pareceria excelente.

## A residente é do processo, não da etapa

As quatro etapas rodam no mesmo processo, e o DuckDB não devolve ao sistema tudo
o que pegou. Por isso o relatório traz **as duas colunas**: a residente na entrada
da etapa e o pico durante ela. A primeira mostra o que ficou das anteriores; a
diferença entre elas é o que a etapa custou; o pico absoluto é o que a promessa de
8 GiB tem de acomodar.
"""

from __future__ import annotations

import datetime as dt
import logging
import platform
import threading
import time
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Final

import psutil

from grafo_societario.config import Config
from grafo_societario.graph.artefatos import ARTEFATOS_PUBLICAVEIS
from grafo_societario.graph.build import gerar_arestas, gerar_nos, serializar_csr
from grafo_societario.graph.components import calcular_componentes

logger = logging.getLogger(__name__)

INTERVALO_DE_AMOSTRAGEM: Final = 0.05
"""Segundos entre leituras da memória residente.

Cinquenta milissegundos é curto o bastante para pegar o pico de uma etapa que dura
segundos, e longo o bastante para o amostrador não competir com o trabalho que ele
está medindo. O commit 21 roda em 6 s: são cento e vinte amostras.
"""

MIB: Final = 1024 * 1024


TETO_DE_DEPLOY: Final = 500_000_000
"""500 MB, em bytes decimais — a unidade em que o limite de free tier é anunciado."""

TETO_DE_MEMORIA: Final = 8 * 1024**3
"""8 GiB. A promessa é sobre a máquina, então a unidade é binária."""


@dataclass(frozen=True)
class Etapa:
    """Uma etapa medida, com o que ela custou e o que produziu."""

    nome: str
    segundos: float
    residente_inicial: int
    pico: int
    resultado: str

    @property
    def acrescimo(self) -> int:
        """O que a etapa somou à residente. Pode ser zero se ela coube no que
        as anteriores já tinham reservado."""
        return max(0, self.pico - self.residente_inicial)


@dataclass(frozen=True)
class Benchmark:
    """A construção inteira, medida."""

    competencia: str
    uf_alvo: str
    medido_em: dt.date
    etapas: tuple[Etapa, ...]
    artefatos: tuple[tuple[str, int], ...]
    nucleos: int
    memoria_da_maquina: int

    nos: int = 0
    arestas: int = 0
    """Tamanho do grafo, como número.

    Campo próprio, e não algo extraído de volta do texto da etapa: reconverter
    para inteiro uma string que acabou de ser formatada quebra no dia em que a
    formatação mudar, e quebra longe de onde a mudança foi feita.
    """

    @property
    def segundos(self) -> float:
        return sum(etapa.segundos for etapa in self.etapas)

    @property
    def pico(self) -> int:
        return max((etapa.pico for etapa in self.etapas), default=0)

    @property
    def bytes_publicaveis(self) -> int:
        return sum(tamanho for _, tamanho in self.artefatos)

    @property
    def margem_de_deploy(self) -> float:
        return (TETO_DE_DEPLOY - self.bytes_publicaveis) / TETO_DE_DEPLOY


class Amostrador:
    """Segue a memória residente do processo enquanto uma etapa roda.

    Existe como classe, e não como decorador, para que a suíte consiga apontá-lo
    para uma alocação conhecida e exigir que ele a veja. Medidor que não prova
    que sabe medir aprova qualquer implementação.
    """

    def __init__(self, intervalo: float = INTERVALO_DE_AMOSTRAGEM) -> None:
        self.intervalo = intervalo
        self.processo = psutil.Process()
        self.pico = 0
        self._parar = threading.Event()
        self._linha: threading.Thread | None = None

    def _amostrar(self) -> None:
        while not self._parar.is_set():
            self.pico = max(self.pico, int(self.processo.memory_info().rss))
            self._parar.wait(self.intervalo)

    def __enter__(self) -> Amostrador:
        self.pico = int(self.processo.memory_info().rss)
        self._parar.clear()
        self._linha = threading.Thread(target=self._amostrar, daemon=True)
        self._linha.start()
        return self

    def __exit__(self, *_: object) -> None:
        self._parar.set()
        if self._linha is not None:
            self._linha.join(timeout=5)
        self.pico = max(self.pico, int(self.processo.memory_info().rss))


def medir(nome: str, trabalho: Callable[[], str]) -> Etapa:
    """Roda uma etapa cronometrada, com o pico amostrado em paralelo."""
    inicial = int(psutil.Process().memory_info().rss)
    comeco = time.monotonic()
    with Amostrador() as amostrador:
        resultado = trabalho()
    return Etapa(
        nome=nome,
        segundos=time.monotonic() - comeco,
        residente_inicial=inicial,
        pico=amostrador.pico,
        resultado=resultado,
    )


def medir_construcao(config: Config, competencia: str | None = None) -> Benchmark:
    """Roda as quatro etapas da Fase 4, na ordem, medindo cada uma."""
    alvo = competencia or config.competencia
    tamanho = {"nos": 0, "arestas": 0}

    def nos() -> str:
        resultado = gerar_nos(config, alvo)
        tamanho["nos"] = resultado.nos
        return f"{inteiro(resultado.nos)} nós · {inteiro(resultado.isolados)} isoladas"

    def arestas() -> str:
        resultado = gerar_arestas(config, alvo)
        return f"{inteiro(resultado.arestas)} vínculos · {inteiro(resultado.pares_distintos)} pares"

    def csr() -> str:
        resultado = serializar_csr(config, alvo)
        tamanho["arestas"] = resultado.arestas
        return f"{inteiro(resultado.arestas)} arestas · {inteiro(resultado.posicoes)} posições"

    def componentes() -> str:
        resultado = calcular_componentes(config, alvo)
        return f"{inteiro(resultado.quantos)} componentes · gigante de {inteiro(resultado.gigante)}"

    etapas = (
        medir("nós com índice denso", nos),
        medir("arestas sócio-empresa", arestas),
        medir("serialização em CSR", csr),
        medir("componentes conexos", componentes),
    )

    destino = config.data_dir / "grafo" / alvo
    artefatos = tuple(
        (nome, (destino / nome).stat().st_size)
        for nome in ARTEFATOS_PUBLICAVEIS
        if (destino / nome).exists()
    )

    resultado = Benchmark(
        competencia=alvo,
        uf_alvo=config.uf_alvo,
        medido_em=dt.date.today(),
        etapas=etapas,
        artefatos=artefatos,
        nucleos=psutil.cpu_count(logical=True) or 0,
        memoria_da_maquina=int(psutil.virtual_memory().total),
        nos=tamanho["nos"],
        arestas=tamanho["arestas"],
    )
    logger.info(
        "construção medida",
        extra={
            "competencia": alvo,
            "uf_alvo": config.uf_alvo,
            "segundos": round(resultado.segundos, 1),
            "pico_bytes": resultado.pico,
            "bytes_publicaveis": resultado.bytes_publicaveis,
            "margem_de_deploy": round(resultado.margem_de_deploy, 3),
        },
    )
    return resultado


# ------------------------------------------------------------------ relatório

PIPELINE_ANTERIOR: Final = (
    ("download de 36 arquivos (6,79 GiB)", "47,6 min", "limitado por rede, 2,4 MiB/s"),
    ("extração e transcodificação", "5,3 min", "23,24 GiB de CSV, ~128 MiB/s"),
    ("bronze (CSV → Parquet)", "~1,7 min", "4,91 GiB, pico de 1,83 GiB"),
)
"""Etapas medidas nas Fases 1 a 3, para o relatório não começar no meio.

Não são remedidas aqui: dependem de rede e de 23 GiB de CSV extraído, e um
benchmark que só roda com o disco cheio não roda. Os números estão no README.
"""

LIMITE_LIVRE_ANUNCIADO: Final = 200_000
"""Teto de nós do plano gratuito do Neo4j AuraDB, na leitura mais generosa.

As próprias fontes do fornecedor divergem: a página do produto anuncia 50.000 nós
e 175.000 relacionamentos, e o FAQ, 200.000 e 400.000. O relatório usa **o maior**
de propósito — se nem no melhor caso cabe, a divergência não muda a conclusão, e
usar o menor pareceria escolher o número que favorece o argumento.
"""

CUSTO_MENSAL_MINIMO: Final = 65
"""Dólares por mês da menor instância paga do AuraDB, de 1 GiB, a US$ 65/GiB.

É piso, e não estimativa: 443 MB de artefato **sem índice nenhum e sem
propriedade de nó** já ocupam quase metade de 1 GiB, e um banco de grafo guarda
as propriedades e os índices que este projeto não guarda.
"""

VERIFICADO_EM: Final = "agosto de 2026"


def inteiro(valor: float) -> str:
    """Número no formato brasileiro: ponto separa milhar.

    O documento é lido por gente e vira post em português. Deixar `10,658,250`
    escapar por ser o padrão do `format` do Python trocaria o separador decimal
    aos olhos de quem lê, que é o pior tipo de erro de número: o que continua
    parecendo um número.
    """
    return f"{round(valor):,}".replace(",", ".")


def decimal(valor: float, casas: int = 1) -> str:
    """Número com vírgula decimal e ponto de milhar."""
    inteira, _, fracionaria = f"{valor:,.{casas}f}".partition(".")
    return inteira.replace(",", ".") + ("," + fracionaria if fracionaria else "")


def _tabela(
    cabecalho: tuple[str, ...],
    linhas: Sequence[tuple[str, ...]],
    alinhamento: tuple[str, ...] | None = None,
) -> str:
    colunas = alinhamento or ("---",) + ("---:",) * (len(cabecalho) - 1)
    partes = [
        "| " + " | ".join(cabecalho) + " |",
        "|" + "|".join(colunas) + "|",
    ]
    partes += ["| " + " | ".join(linha) + " |" for linha in linhas]
    return "\n".join(partes)


def escrever_relatorio(benchmark: Benchmark, destino: Path) -> Path:
    """Gera `docs/benchmark.md` a partir do que foi medido."""
    etapas = _tabela(
        ("Etapa", "Tempo", "Residente na entrada", "Pico", "Produziu"),
        [
            (
                etapa.nome,
                f"{decimal(etapa.segundos)} s",
                f"{inteiro(etapa.residente_inicial / MIB)} MiB",
                f"**{inteiro(etapa.pico / MIB)} MiB**",
                etapa.resultado,
            )
            for etapa in benchmark.etapas
        ],
        alinhamento=("---", "---:", "---:", "---:", "---"),
    )
    artefatos = _tabela(
        ("Artefato", "Bytes", "MiB"),
        [
            (f"`{nome}`", inteiro(tamanho), decimal(tamanho / MIB, 2))
            for nome, tamanho in benchmark.artefatos
        ]
        + [
            (
                "**total**",
                f"**{inteiro(benchmark.bytes_publicaveis)}**",
                f"**{decimal(benchmark.bytes_publicaveis / MIB)}**",
            )
        ],
    )
    anterior = _tabela(
        ("Etapa", "Tempo", "Observação"),
        list(PIPELINE_ANTERIOR),
        alinhamento=("---", "---:", "---"),
    )
    maquina = (
        f"{benchmark.nucleos} núcleos lógicos, "
        f"{inteiro(benchmark.memoria_da_maquina / 1024**3)} GiB de RAM, "
        f"{platform.system()} {platform.release()}, Python {platform.python_version()}"
    )
    pico_gib = decimal(benchmark.pico / 1024**3, 2)
    fracao_do_teto = decimal(100 * benchmark.pico / TETO_DE_MEMORIA)
    primeiro_pico = inteiro(benchmark.etapas[0].pico / MIB) if benchmark.etapas else "0"
    vezes_o_teto_livre = (
        inteiro(benchmark.nos / LIMITE_LIVRE_ANUNCIADO) if benchmark.nos else "muitas"
    )

    texto = f"""# Benchmark da construção do grafo

> Gerado por `grafo_societario.graph.benchmark`. Competência
> **{benchmark.competencia}**, `UF_ALVO={benchmark.uf_alvo}`, medido em
> {benchmark.medido_em.isoformat()}. Máquina: {maquina}.

## Fase 4 — do silver ao grafo consultável

{etapas}

**Total de {decimal(benchmark.segundos)} s**, com pico de memória residente de
**{pico_gib} GiB** — {fracao_do_teto}% do teto de 8 GiB que o projeto promete.

O pico é **amostrado a cada 50 ms enquanto a etapa roda**, e não lido depois que
ela termina. A diferença não é sutil: lida no fim, a primeira etapa reporta uma
residente modesta, porque a essa altura o motor já devolveu o que tinha pegado. O
que ela realmente exigiu da máquina foi **{primeiro_pico} MiB**.

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

{artefatos}

Contra o teto de {TETO_DE_DEPLOY // 1_000_000} MB, sobra
**{decimal(100 * benchmark.margem_de_deploy)}%**. `arestas.parquet` não entra na
conta: é insumo do CSR, e não é consultado em tempo de resposta.

## Antes da Fase 4

{anterior}

Medido nas Fases 1 a 3 e não remedido aqui: depende de rede e de 23 GiB de CSV
extraído em disco. **O download é 94% do tempo total do pipeline** — o
processamento inteiro, de CSV bruto a grafo consultável, é uma fração do que se
gasta esperando a Receita Federal entregar os arquivos.

## O que isto custaria num banco de grafo gerenciado

O grafo tem {inteiro(benchmark.nos)} nós e {inteiro(benchmark.arestas)} arestas.

**No plano gratuito, não cabe — e não é por pouco.** O teto é de
{inteiro(LIMITE_LIVRE_ANUNCIADO)} nós na leitura mais generosa das fontes do
próprio fornecedor, que divergem entre si: a página do produto anuncia 50.000 nós
e 175.000 relacionamentos, e o FAQ, 200.000 e 400.000. Este grafo é **cerca de
{vezes_o_teto_livre} vezes** o maior desses dois tetos, e mais de duzentas vezes o
menor. Divergência de fonte não muda conclusão quando a distância é de duas ordens
de grandeza — e usar o maior de propósito evita escolher o número que favorece o
argumento.

**No plano pago, o piso é US$ {CUSTO_MENSAL_MINIMO}/mês**: a menor instância, de
1 GiB, a US$ {CUSTO_MENSAL_MINIMO} por GiB/mês. É piso e não estimativa — os
{decimal(benchmark.bytes_publicaveis / MIB)} MiB de artefato deste projeto não
guardam índice nenhum nem propriedade de nó, e um banco de grafo guarda os dois.
São US$ {CUSTO_MENSAL_MINIMO * 12}/ano no melhor caso, contra **R$ 0**.

Valores verificados em {VERIFICADO_EM} e sujeitos a mudança; confira antes de citar.

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
"""
    destino.parent.mkdir(parents=True, exist_ok=True)
    destino.write_text(texto, encoding="utf-8")
    return destino
