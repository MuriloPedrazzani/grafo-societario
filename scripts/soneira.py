"""Soneira contra uma instância no ar: a residente estabiliza, ou sobe?

## A pergunta, e por que ela não é sobre consulta cara

O acervo é lido com `mmap`, então **cada página tocada fica residente**. O risco
contra os 512 MB do free tier não é uma consulta cara — uma travessia ao
componente gigante custa ~92 MB, e uma requisição rejeitada com `422`, que não
toca o grafo, deixa a residente praticamente no mesmo lugar. As duas descrevem
residência de base.

O risco é a residente subir com a **cobertura**: consultas diversas, ao longo do
tempo, alcançando regiões novas do grafo. Página limpa de arquivo é recuperável
sob pressão, então a expectativa é que a curva estabilize — e é exatamente a
expectativa que esta soneira existe para substituir.

## Este é o lugar onde cronometrar HTTP mora

Antes deste script, todas as medições de latência do projeto foram **ad hoc**, e
uma delas cronometrou um `422` como se fosse travessia: o parâmetro estava errado,
o servidor recusou, e ninguém olhou o status porque o tempo pareceu plausível.

`pedir` **afirma o status antes de registrar o tempo**. Cronometrar resposta que
não é a esperada é a versão HTTP do instrumento que passa por não olhar — ele
devolve um número, e número errado se propaga como se fosse medição.

## Uso

    python scripts/soneira.py --url https://exemplo.onrender.com --segundos 300
"""

from __future__ import annotations

import argparse
import json
import random
import re
import sys
import time
import urllib.error
import urllib.request
from dataclasses import dataclass
from pathlib import Path

RAIZ = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(RAIZ / "src"))

import numpy as np  # noqa: E402

from grafo_societario.api.cnpj import formatar  # noqa: E402

# ------------------------------------------------- o critério de parada
#
# Declarado aqui, em código, e não decidido ao olhar a curva. Sem critério
# anterior, "estabilizou" quer dizer "eu cansei", e a conclusão vira função da
# paciência de quem rodou.

COBERTURA_MINIMA = 2_000
"""CNPJs distintos consultados antes de a estabilidade poder ser afirmada.

Sem piso de cobertura, um platô nos primeiros dez segundos passaria por resposta
— e ele só diria que ainda não se tocou em página nova."""

PLATO_AMOSTRAS = 6
"""Amostras seguidas dentro da faixa. A 5 s cada, são 30 s de platô."""

PLATO_VARIACAO = 2 * 1024 * 1024
"""Faixa em que as amostras do platô têm de caber: 2 MiB.

Maior que o ruído de alocação do interpretador, e pequeno diante dos 512 MB do
teto — 0,4% dele."""

RITMO_PADRAO = 55
"""Requisições por minuto. O limitador da API aceita 60 por cliente.

A soneira **respeita o limite em vez de contorná-lo**, porque é o que um cliente
real enfrenta. O custo é tempo: a 55/min, alcançar a cobertura mínima leva perto
de vinte minutos.

Quem quiser encurtar sobe `LIMITE_POR_MINUTO` na instância durante a medição — o
que é legítimo aqui, já que residência de página não depende do limitador — e
passa `--por-minuto` igual. O que não vale é o script fingir que o 429 não
aconteceu."""


class RespostaInesperadaError(RuntimeError):
    """O servidor respondeu, e não o que a medição pressupõe."""


@dataclass(frozen=True)
class Amostra:
    segundos_desde_o_inicio: float
    requisicoes: int
    residente_bytes: int | None


def pedir(url: str, esperado: int = 200) -> float:
    """Faz a requisição e devolve os segundos. **Levanta antes de cronometrar
    qualquer coisa que não seja o status esperado.**

    O tempo só é devolvido depois de o status ser conferido. Um `422` responde
    rápido e parece medição boa — foi assim que uma vizinhança de hub entrou no
    ESTADO como se tivesse sido medida.
    """
    comeco = time.perf_counter()
    try:
        with urllib.request.urlopen(url) as resposta:
            resposta.read()
            status = resposta.status
    except urllib.error.HTTPError as erro:
        raise RespostaInesperadaError(
            f"{url} devolveu {erro.code}, e a medição pressupõe {esperado}. Tempo não registrado."
        ) from erro
    if status != esperado:
        raise RespostaInesperadaError(
            f"{url} devolveu {status}, e a medição pressupõe {esperado}. Tempo não registrado."
        )
    return time.perf_counter() - comeco


def residente_do(base: str) -> int | None:
    with urllib.request.urlopen(f"{base}/health") as resposta:
        corpo = json.loads(resposta.read())
    valor = corpo.get("residente_bytes")
    return int(valor) if isinstance(valor, int) else None


def _cnpjs(quantos: int, semente: int) -> list[str]:
    """CNPJs sorteados do próprio artefato, para a cobertura ser do grafo real.

    Lista fixa de exemplos cobriria sempre as mesmas páginas, e a pergunta é
    justamente sobre alcançar páginas novas.
    """
    caminho = RAIZ / "data" / "grafo" / "2026-06" / "cnpj_ordenado.npy"
    if not caminho.exists():
        raise SystemExit(
            f"{caminho} não existe. A soneira sorteia CNPJ do artefato local para "
            f"cobrir o grafo — construa ou extraia a Release antes."
        )
    ordenados = np.load(caminho, mmap_mode="r")
    sorteio = random.Random(semente)
    indices = sorteio.sample(range(int(ordenados.size)), min(quantos, int(ordenados.size)))
    # O artefato guarda `cnpj_basico`, oito dígitos, e o endpoint quer o CNPJ
    # completo com verificador. `formatar` é a função que a própria API usa —
    # reimplementar o dígito aqui seria a segunda implementação do mesmo cálculo.
    #
    # Sem a máscara: `formatar` devolve `00.160.109/0001-96`, e a barra parte a
    # rota `/empresa/{cnpj}` em dois segmentos. O servidor responde 404, que
    # significa "não conheço esta empresa" e faria a soneira concluir que o
    # artefato está incompleto.
    return [re.sub(r"\D", "", formatar(int(ordenados[i]))) for i in indices]


def _estabilizou(amostras: list[Amostra]) -> bool:
    medidas = [a.residente_bytes for a in amostras if a.residente_bytes is not None]
    if len(medidas) < PLATO_AMOSTRAS:
        return False
    ultimas = medidas[-PLATO_AMOSTRAS:]
    return max(ultimas) - min(ultimas) < PLATO_VARIACAO


def rodar(
    base: str, segundos: int, semente: int, por_minuto: int = RITMO_PADRAO
) -> tuple[list[Amostra], str, int]:
    """Devolve as amostras, **o motivo da parada** e quantos CNPJs distintos foram
    consultados.

    O motivo importa tanto quanto a curva: parar por tempo com a residente ainda
    subindo é "não estabilizou no tempo medido", e não "estabilizou"."""
    cnpjs = _cnpjs(5_000, semente)
    sorteio = random.Random(semente)
    amostras: list[Amostra] = []
    tocados: set[str] = set()
    inicio = time.perf_counter()
    feitas = 0
    proxima_amostra = 0.0
    intervalo = 60.0 / por_minuto

    while True:
        decorrido = time.perf_counter() - inicio
        if decorrido >= proxima_amostra:
            amostras.append(Amostra(decorrido, feitas, residente_do(base)))
            proxima_amostra = decorrido + 5.0
            if len(tocados) >= COBERTURA_MINIMA and _estabilizou(amostras):
                return amostras, "platô com cobertura", len(tocados)
        if decorrido >= segundos:
            motivo = (
                "tempo esgotado, cobertura insuficiente"
                if len(tocados) < COBERTURA_MINIMA
                else "tempo esgotado SEM platô — a residente ainda subia"
            )
            amostras.append(Amostra(decorrido, feitas, residente_do(base)))
            return amostras, motivo, len(tocados)

        a, b = sorteio.sample(cnpjs, 2)
        tocados.update((a, b))
        escolha = sorteio.random()
        if escolha < 0.4:
            pedir(f"{base}/empresa/{a}")
        elif escolha < 0.7:
            pedir(f"{base}/vizinhanca?cnpj={a}&saltos=2")
        else:
            pedir(f"{base}/caminho?de={a}&para={b}&profundidade_maxima=10")
        feitas += 1

        # Espera o que falta para o ritmo combinado. Dormir depois da requisição,
        # e não antes, faz o intervalo contar do início dela — senão o tempo de
        # resposta somaria ao ritmo e a soneira ficaria mais lenta que o pedido.
        atraso = inicio + feitas * intervalo - time.perf_counter()
        if atraso > 0:
            time.sleep(atraso)


def main() -> int:
    analisador = argparse.ArgumentParser(description=__doc__)
    analisador.add_argument("--url", required=True, help="base da instância, sem barra final")
    analisador.add_argument("--segundos", type=int, default=300)
    analisador.add_argument("--semente", type=int, default=42)
    analisador.add_argument(
        "--por-minuto",
        type=int,
        default=RITMO_PADRAO,
        help=f"ritmo de requisições; o limitador da API aceita 60 (padrão {RITMO_PADRAO})",
    )
    opcoes = analisador.parse_args()

    base = opcoes.url.rstrip("/")
    print(f"soneira contra {base} por {opcoes.segundos}s, a {opcoes.por_minuto}/min\n")
    amostras, motivo, tocados = rodar(base, opcoes.segundos, opcoes.semente, opcoes.por_minuto)

    print(f"{'t (s)':>8} {'requisições':>12} {'residente':>14}")
    for amostra in amostras:
        residente = (
            f"{amostra.residente_bytes / 1048576:.1f} MiB"
            if amostra.residente_bytes is not None
            else "nulo"
        )
        print(f"{amostra.segundos_desde_o_inicio:8.1f} {amostra.requisicoes:12d} {residente:>14}")

    medidas = [a.residente_bytes for a in amostras if a.residente_bytes is not None]
    if len(medidas) < 2:
        print("\nsem medida de residente: a instância não expõe `/proc`.")
        return 0
    print(f"\nprimeira: {medidas[0] / 1048576:.1f} MiB · última: {medidas[-1] / 1048576:.1f} MiB")
    print(f"máximo:   {max(medidas) / 1048576:.1f} MiB")
    print(f"variação: {(medidas[-1] - medidas[0]) / 1048576:+.1f} MiB")
    print(f"\nparada:    {motivo}")
    print(f"cobertura: {tocados} CNPJs distintos (piso {COBERTURA_MINIMA})")
    if motivo != "platô com cobertura":
        print("\nA curva NÃO satisfez o critério declarado. Isto não é 'estabilizou'.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
