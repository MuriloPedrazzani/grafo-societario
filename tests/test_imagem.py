"""A imagem de serving, e as propriedades dela que não podem se perder.

## Por que guardas de texto, e não um build no CI

Construir a imagem custa baixar 235 MB da Release e exportar 1,1 GB de camadas.
Fazer isso a cada execução do CI pagaria alto por uma pergunta que o deploy
responde melhor: o critério do commit 42 é **partida fria medida**, e ela só
existe na instância.

O que estas guardas protegem é outra coisa — as decisões que, se forem desfeitas
numa limpeza, produzem uma imagem que **constrói e sobe normalmente** e falha só
em produção, ou só depois de a instância hibernar. São exatamente as que uma
revisão de Dockerfile não pega: `curl` no `CMD` parece razoável, `chown -R`
parece equivalente a `--chown`, e porta fixa parece mais simples que `${PORT}`.

A imagem foi construída e exercida à mão neste commit: sobe com limite de 512 MB,
responde `/health` em 25 ms, serve a página e os estáticos, e uma travessia ao
componente gigante leva o RSS de 197,6 MB a 291,7 MB.
"""

from __future__ import annotations

import re
from pathlib import Path

RAIZ = Path(__file__).resolve().parents[1]
DOCKERFILE = RAIZ / "Dockerfile"
DOCKERIGNORE = RAIZ / ".dockerignore"


def _sem_comentarios(texto: str) -> str:
    """O Dockerfile é mais comentário que instrução, e os comentários citam
    justamente as coisas que as guardas procuram — `chown -R`, `curl`, download
    em runtime. Procurar no texto cru daria positivo na explicação do porquê."""
    return "\n".join(linha for linha in texto.splitlines() if not linha.lstrip().startswith("#"))


def _estagio_final(texto: str) -> str:
    """Só o último estágio vira imagem. O que os outros instalam fica para trás,
    e é essa a razão de eles existirem."""
    partes = re.split(r"^FROM ", _sem_comentarios(texto), flags=re.MULTILINE)
    return partes[-1]


def test_nada_e_baixado_em_tempo_de_execucao() -> None:
    """O artefato tem 416,1 MB. Num free tier que hiberna, baixá-lo no boot
    significa baixá-lo a cada despertar, e a demonstração quebra no caso que mais
    importa: quem abre o link pela primeira vez.

    A guarda é sobre a instrução de partida: se `curl` ou `wget` aparecerem no
    `CMD` ou no `ENTRYPOINT`, o download voltou para o runtime.
    """
    conteudo = _sem_comentarios(DOCKERFILE.read_text(encoding="utf-8"))

    partida = [linha for linha in conteudo.splitlines() if linha.startswith(("CMD", "ENTRYPOINT"))]
    assert partida, "a imagem precisa declarar CMD ou ENTRYPOINT"
    for linha in partida:
        assert "curl" not in linha and "wget" not in linha, (
            f"download em tempo de execução: {linha!r}. O artefato entra na imagem "
            f"em tempo de build — se ele voltar para o boot, cada despertar da "
            f"instância gratuita paga 416 MB antes da primeira resposta."
        )


def test_a_soma_do_artefato_e_conferida_antes_de_extrair() -> None:
    """Artefato corrompido tem de derrubar o build, não virar resposta errada.

    A ordem importa: conferir depois de extrair deixaria os arquivos no lugar
    enquanto o build falha, e uma camada com dado ruim é pior que nenhuma.
    """
    conteudo = _sem_comentarios(DOCKERFILE.read_text(encoding="utf-8"))

    assert "sha256sum -c" in conteudo, "a soma da Release não é conferida"
    assert conteudo.index("sha256sum -c") < conteudo.index("tar -xzf"), (
        "a conferência da soma tem de vir antes da extração"
    )


def test_o_artefato_e_copiado_com_chown_e_nunca_com_chown_recursivo() -> None:
    """`chown -R` depois da cópia cria uma camada nova com **todos** os arquivos
    duplicados. Com 416 MB de artefato, isso dobra a imagem.

    A diferença não aparece em teste de funcionamento — a imagem sobe e responde
    igual, só ocupa o dobro. Aparece no teto de tamanho da plataforma, que não é
    documentado, no pior momento possível.
    """
    conteudo = _sem_comentarios(DOCKERFILE.read_text(encoding="utf-8"))

    copia = [
        linha
        for linha in conteudo.splitlines()
        if linha.startswith("COPY") and "artefatos" in linha
    ]
    assert copia, "o artefato não é copiado para a imagem final"
    for linha in copia:
        assert "--chown=" in linha, f"cópia do artefato sem --chown: {linha!r}"

    assert not re.search(r"chown\s+-R", conteudo), (
        "`chown -R` duplica os arquivos numa camada nova; use `--chown=` no COPY"
    )


def test_a_porta_vem_do_ambiente() -> None:
    """O free tier do Render injeta `PORT` e espera que o processo escute nela.
    Porta fixa no `CMD` sobe local e não recebe tráfego nenhum no deploy."""
    conteudo = _sem_comentarios(DOCKERFILE.read_text(encoding="utf-8"))

    assert "${PORT" in conteudo, "a porta precisa sair da variável PORT do ambiente"


def test_a_imagem_final_nao_carrega_gerenciador_de_pacotes() -> None:
    """`pip` e `setuptools` são 28 MB que não respondem consulta nenhuma, e são
    ferramenta a menos para quem conseguir executar código dentro do contêiner.

    Eles saem **na mesma camada** em que entram: apagar num `RUN` separado não
    adiantaria, porque a camada anterior guardaria os arquivos de qualquer jeito.
    """
    conteudo = _sem_comentarios(DOCKERFILE.read_text(encoding="utf-8"))

    instala = next((bloco for bloco in conteudo.split("RUN ") if "pip install" in bloco), None)
    assert instala is not None, "o Dockerfile não instala o pacote"
    assert "site-packages/pip" in instala, (
        "pip precisa ser removido na mesma instrução RUN que o instala"
    )


def test_o_usuario_nao_e_root() -> None:
    conteudo = _sem_comentarios(DOCKERFILE.read_text(encoding="utf-8"))
    final = _estagio_final(conteudo)

    assert re.search(r"^USER \w+", final, flags=re.MULTILINE), (
        "a imagem final precisa trocar de usuário; root é o padrão do Docker"
    )
    assert not re.search(r"^USER root", final, flags=re.MULTILINE)


def test_o_contexto_de_build_exclui_os_dados() -> None:
    """`data/` tem 662 MB nesta máquina e nada dele entra na imagem — os
    artefatos vêm da Release. Sem a exclusão o contexto inteiro sobe para o
    daemon, e o build passa a depender do que existe na máquina de quem constrói.
    """
    conteudo = DOCKERIGNORE.read_text(encoding="utf-8")
    linhas = {linha.strip() for linha in conteudo.splitlines()}

    assert "data/" in linhas, "o contexto de build precisa excluir `data/`"
    assert ".venv/" in linhas, "o contexto de build precisa excluir `.venv/`"


def test_as_guardas_do_dockerfile_sabem_reprovar() -> None:
    """Controle positivo: guarda que só compara string passa fácil demais.

    Se estas procuras não reprovassem o que deveriam, seriam linhas verdes
    permanentes dando impressão de cobertura.
    """
    conteudo = _sem_comentarios(DOCKERFILE.read_text(encoding="utf-8"))

    assert "instrucao_que_nao_existe" not in conteudo
    assert not re.search(r"chown\s+-R", conteudo)
    assert _estagio_final("FROM a\nUSER root\n").strip() == "a\nUSER root"
