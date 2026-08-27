# syntax=docker/dockerfile:1
#
# A imagem de serving, e por que os artefatos entram aqui em vez de serem
# baixados quando a instância acorda.
#
# ## Nada é baixado em tempo de execução
#
# O artefato publicável tem 416,1 MB. Num free tier que hiberna, baixá-lo no
# boot significa baixá-lo *a cada despertar*, e a demonstração quebraria
# exatamente no caso que importa: quem abre o link pela primeira vez, vindo de
# um currículo, sem paciência acumulada.
#
# O que está na imagem sobrevive à hibernação, porque o contêiner reinicia a
# partir dela. É isso que dispensa volume persistente — que nenhum free tier
# oferece — e o que revogou o teto de 300 MB que o plano original previa. O teto
# que vale agora é o que a plataforma impuser, e ele aparece aqui, no build, que
# é o lugar certo para aparecer.
#
# ## A origem é a Release, e ela é conferida
#
# O GitHub Release continua sendo a fonte única do artefato: é o que o README
# manda baixar e o que o `429` da API aponta. A imagem consome a mesma coisa que
# um humano consumiria, e confere a soma antes de extrair — artefato que chega
# corrompido tem de derrubar o build, não virar resposta errada em produção.

ARG PYTHON_VERSION=3.11
ARG COMPETENCIA=2026-06

# --------------------------------------------------------------- artefatos
# Estágio próprio para o download: o que ele instala (curl, ca-certificates) não
# tem por que existir na imagem final, e o tarball baixado não pode sobreviver
# como camada — ele dobraria o tamanho, já que os arquivos extraídos ficam.
FROM debian:bookworm-slim AS artefatos

ARG COMPETENCIA
ARG ARTEFATOS_BASE_URL=https://github.com/MuriloPedrazzani/grafo-societario/releases/download/artefatos-2026-06

RUN apt-get update \
    && apt-get install -y --no-install-recommends curl ca-certificates \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /trabalho

# Download, conferência e extração numa camada só. Se fossem três instruções, o
# tarball ficaria numa camada intermediária e continuaria pesando na imagem
# mesmo depois do `rm` — camada não se desfaz.
RUN set -eux; \
    arquivo="artefatos-${COMPETENCIA}.tar.gz"; \
    curl -fsSL -o "${arquivo}"        "${ARTEFATOS_BASE_URL}/${arquivo}"; \
    curl -fsSL -o "${arquivo}.sha256" "${ARTEFATOS_BASE_URL}/${arquivo}.sha256"; \
    sha256sum -c "${arquivo}.sha256"; \
    mkdir -p "/artefatos/grafo/${COMPETENCIA}"; \
    tar -xzf "${arquivo}" -C "/artefatos/grafo/${COMPETENCIA}"; \
    rm -f "${arquivo}" "${arquivo}.sha256"

# --------------------------------------------------------------- construção
# O `pip` e o cache de roda ficam neste estágio. A imagem final recebe só o
# ambiente virtual pronto, sem gerenciador de pacotes nem cabeçalho de compilação.
FROM python:${PYTHON_VERSION}-slim AS construcao

ENV PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    PIP_ROOT_USER_ACTION=ignore

RUN python -m venv /opt/venv
ENV PATH="/opt/venv/bin:${PATH}"

WORKDIR /src
COPY pyproject.toml README.md ./
COPY src ./src

# Sem `[build]` e sem `[dev]`: o serving não importa SciPy nem pytest. O extra
# `build` existe exatamente para essa separação, e é aqui que ela vira tamanho de
# imagem em vez de intenção documentada.
#
# `pip` e `setuptools` saem na mesma camada em que entram: instalar dentro do
# ambiente virtual os deixa lá, e 28 MB de gerenciador de pacotes num contêiner
# que só responde consulta é peso morto — além de ferramenta a menos para quem
# conseguir executar código dentro dele. Apagar em RUN separado não adiantaria:
# a camada anterior guardaria os arquivos de qualquer jeito.
RUN pip install --no-cache-dir . \
    && rm -rf /opt/venv/lib/python*/site-packages/pip \
              /opt/venv/lib/python*/site-packages/pip-*.dist-info \
              /opt/venv/lib/python*/site-packages/setuptools \
              /opt/venv/lib/python*/site-packages/setuptools-*.dist-info \
              /opt/venv/lib/python*/site-packages/pkg_resources \
              /opt/venv/bin/pip /opt/venv/bin/pip3 /opt/venv/bin/pip3.*

# --------------------------------------------------------------- final
FROM python:${PYTHON_VERSION}-slim AS final

ARG COMPETENCIA

# Usuário sem privilégio, com UID alto e fixo. Fixo porque o `--chown` abaixo
# precisa de um número estável, e alto para não colidir com usuário de sistema
# de uma imagem base futura.
RUN useradd --create-home --uid 10001 grafo

ENV PATH="/opt/venv/bin:${PATH}" \
    PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    COMPETENCIA=${COMPETENCIA} \
    UF_ALVO=SP \
    DATA_DIR=/app/data

COPY --from=construcao /opt/venv /opt/venv
# `--chown` na cópia, nunca `chown -R` depois: alterar dono de arquivo já
# copiado cria uma camada nova com o arquivo inteiro duplicado, e com 416 MB
# isso dobraria a imagem.
COPY --from=artefatos --chown=grafo:grafo /artefatos /app/data

WORKDIR /app
USER grafo

# O free tier do Render injeta `PORT` e espera que o processo escute nela. O
# padrão local é 8000 para o contêiner subir sem variável nenhuma.
EXPOSE 8000
CMD ["sh", "-c", "exec uvicorn grafo_societario.api.main:app --host 0.0.0.0 --port ${PORT:-8000}"]
