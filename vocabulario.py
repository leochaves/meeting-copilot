"""Vocabulário e contexto do usuário (tela Ajustes → Vocabulário e contexto).

Três ajudas pra o app errar menos nomes e termos técnicos:
- `termos`: dica pra transcrição (Whisper/Deepgram) e pro modelo de linguagem;
- `correcoes`: trocas aplicadas no texto transcrito antes de salvar;
- `contexto`: texto livre sobre o usuário, lido pelo modelo de linguagem.
"""

import re

# O Whisper só considera ~224 tokens de prompt; deixa folga
MAX_PROMPT_WHISPER = 600


def termos(texto: str) -> list[str]:
    """Um termo por linha ou separados por vírgula, sem repetir."""
    vistos, lista = set(), []
    for t in re.split(r"[,\n;]", texto or ""):
        t = t.strip()
        if t and t.lower() not in vistos:
            vistos.add(t.lower())
            lista.append(t)
    return lista


def correcoes(texto: str) -> list[tuple[re.Pattern, str]]:
    """Linhas `errado => certo` (também aceita -> e →). Vale palavra inteira,
    sem diferenciar maiúsculas."""
    regras = []
    for linha in (texto or "").splitlines():
        partes = re.split(r"\s*(?:=>|->|→)\s*", linha.strip(), maxsplit=1)
        if len(partes) == 2 and partes[0] and partes[1]:
            padrao = re.compile(r"(?<!\w)" + re.escape(partes[0]) + r"(?!\w)", re.IGNORECASE)
            regras.append((padrao, partes[1]))
    return regras


def corrigir(texto: str, regras: list[tuple[re.Pattern, str]]) -> str:
    for padrao, certo in regras:
        texto = padrao.sub(certo, texto)
    return texto


def prompt_whisper(lista: list[str]) -> str | None:
    """Prompt inicial do Whisper: ele tende a escrever do jeito que vê aqui."""
    if not lista:
        return None
    prompt = "Termos: " + ", ".join(lista) + "."
    return prompt[:MAX_PROMPT_WHISPER]


def bloco_sistema(contexto: str, lista: list[str], assunto: str = "") -> str:
    """Trecho acrescentado ao system prompt do modelo de linguagem.

    O contexto é material de referência, não conteúdo: sem esse aviso,
    modelos pequenos montam a ata a partir dele quando a transcrição é fraca
    (ex.: listam como "tarefas" as funções das pessoas descritas aqui).
    """
    partes = []
    if (contexto or "").strip():
        partes.append(f"Sobre o usuário:\n{contexto.strip()}")
    if lista:
        partes.append(
            "Grafias corretas de nomes e termos (a transcrição automática pode "
            "escrever errado; quando algo soar parecido, use esta grafia): "
            + ", ".join(lista) + "."
        )
    if (assunto or "").strip():
        partes.append(
            f"Assunto previsto desta reunião, informado pelo usuário: {assunto.strip()}\n"
            "(é só a expectativa — confira com o que for de fato dito)."
        )
    if not partes:
        return ""
    aviso = (
        "## Material de referência — NÃO é o conteúdo da reunião\n"
        "Use o que vem abaixo só para reconhecer nomes, siglas e grafias e para "
        "entender termos. Nunca tire daqui fatos, assuntos, decisões, tarefas ou "
        "responsáveis: eles só valem se forem ditos na transcrição. Se a "
        "transcrição não falar de um projeto ou pessoa listado aqui, não o mencione."
    )
    return aviso + "\n\n" + "\n\n".join(partes)
