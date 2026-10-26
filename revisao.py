"""Revisão pós-reunião: o usuário conversa com a IA pra corrigir a transcrição
e a ata, e a IA aponta o que vale guardar no contexto (Ajustes) pras próximas.

A IA só propõe — nada muda até o usuário clicar em aplicar.
"""

import re

import vocabulario
from sessao import mmss

SISTEMA_REVISAO = """Você ajuda o usuário a revisar uma reunião já encerrada. \
O material abaixo tem a transcrição automática (com erros de reconhecimento: \
nomes de pessoas, siglas e termos técnicos costumam sair errados), a ata e o \
contexto que o usuário já cadastrou. "Você" na transcrição é o usuário.

O usuário vai apontar erros e pedir mudanças. Responda sempre no formato JSON \
pedido, em português do Brasil:

- resposta: curta. Diga o que você entendeu e o que propõe. Se faltar \
informação pra corrigir com segurança, pergunte aqui e deixe o resto vazio.
- substituicoes: trocas de texto pra aplicar na transcrição e nas explicações \
desta reunião. "de" tem que ser o trecho exatamente como aparece na \
transcrição (copie a grafia errada); "para" é o certo. Inclua cada variação \
errada que aparecer. Vazio se nada mudar.
- nova_ata: a ata inteira reescrita em Markdown, já corrigida, quando algo da \
ata mudar (nomes, responsáveis, decisões). Mantenha o mesmo formato de seções. \
String vazia se a ata não mudar.
- sugestoes_contexto: o que vale guardar pras PRÓXIMAS reuniões. Olhe a \
reunião inteira, não só a correção pedida:
  - termos: grafias corretas de nomes, siglas, sistemas e tecnologias;
  - correcoes: erros de transcrição que tendem a se repetir (como saiu → \
como é);
  - contexto: fatos estáveis, em frases curtas (quem é quem, papel de cada \
pessoa, o que é cada projeto). Nunca decisões, tarefas ou prazos desta reunião.
  Não repita o que já está no contexto atual. Listas vazias se não houver nada.

Nunca invente: use só o que está no material ou o que o usuário disse."""

LEMBRETE = "Responda no JSON pedido. Proponha só o que tiver base no material ou no que eu disse."

_TROCA = {
    "type": "object",
    "properties": {"de": {"type": "string"}, "para": {"type": "string"}},
    "required": ["de", "para"],
    "additionalProperties": False,
}
ESQUEMA = {
    "type": "object",
    "properties": {
        "resposta": {"type": "string"},
        "substituicoes": {"type": "array", "items": _TROCA},
        "nova_ata": {"type": "string"},
        "sugestoes_contexto": {
            "type": "object",
            "properties": {
                "termos": {"type": "array", "items": {"type": "string"}},
                "correcoes": {"type": "array", "items": _TROCA},
                "contexto": {"type": "array", "items": {"type": "string"}},
            },
            "required": ["termos", "correcoes", "contexto"],
            "additionalProperties": False,
        },
    },
    "required": ["resposta", "substituicoes", "nova_ata", "sugestoes_contexto"],
    "additionalProperties": False,
}


def material(reuniao: dict, cfg: dict, max_chars: int | None = None) -> str:
    """Monta o system prompt com a reunião e o contexto atual. `max_chars`
    limita a transcrição (modelos locais têm contexto curto): mantém o começo
    e o fim, que é onde nomes e conclusões costumam aparecer."""
    transcricao = "\n".join(f"[{mmss(f['t'])}] {f['quem']}: {f['texto']}" for f in reuniao["falas"])
    if max_chars and len(transcricao) > max_chars:
        metade = max_chars // 2
        transcricao = transcricao[:metade] + "\n[… trecho do meio omitido …]\n" + transcricao[-metade:]
    partes = [
        SISTEMA_REVISAO,
        f"## Reunião: {reuniao['titulo']}" + (f"\nAssunto: {reuniao['assunto']}" if reuniao.get("assunto") else ""),
        f"## Transcrição\n{transcricao or '(vazia)'}",
        f"## Ata atual\n{reuniao.get('ata') or '(sem ata)'}",
        "## Contexto já cadastrado nos Ajustes",
        f"Nomes e termos: {', '.join(vocabulario.termos(cfg.get('vocabulario', ''))) or '(nenhum)'}",
        f"Correções:\n{(cfg.get('correcoes') or '').strip() or '(nenhuma)'}",
        f"Sobre o usuário:\n{(cfg.get('contexto') or '').strip() or '(vazio)'}",
    ]
    return "\n\n".join(partes)


def historico(revisoes: list[dict], nova_mensagem: str) -> list[dict]:
    """Conversa da revisão no formato de mensagens. As respostas da IA voltam
    como o JSON que ela mesma gerou."""
    import json

    msgs = []
    for r in revisoes:
        if r["papel"] == "usuario":
            msgs.append({"role": "user", "content": r["texto"]})
        else:
            msgs.append({"role": "assistant", "content": json.dumps(r["dados"] or {"resposta": r["texto"]}, ensure_ascii=False)})
    msgs.append({"role": "user", "content": f"{nova_mensagem}\n\n({LEMBRETE})"})
    return msgs


def _trocas(lista) -> list[dict]:
    vistos, saida = set(), []
    for t in lista or []:
        if not isinstance(t, dict):
            continue
        de, para = str(t.get("de", "")).strip(), str(t.get("para", "")).strip()
        if de and para and de != para and de.lower() not in vistos:
            vistos.add(de.lower())
            saida.append({"de": de, "para": para})
    return saida


def _titulos_markdown(ata: str) -> str:
    """Modelos pequenos às vezes trocam `## Resumo` por `**Resumo**`."""
    return re.sub(r"(?m)^[ \t]*\*\*([^*\n]{2,40})\*\*:?[ \t]*$", r"## \1", ata)


def normalizar(dados: dict, cfg: dict | None = None) -> dict:
    """Garante o formato (modelos locais escorregam), completa as sugestões
    que dá pra deduzir e tira o que já está nos Ajustes."""
    cfg = cfg or {}
    sug = dados.get("sugestoes_contexto") or {}
    substituicoes = _trocas(dados.get("substituicoes"))

    ja_termos = {t.lower() for t in vocabulario.termos(cfg.get("vocabulario", ""))}
    ja_correcoes = {
        l.split("=>")[0].strip().lower() for l in (cfg.get("correcoes") or "").splitlines() if "=>" in l
    }
    contexto_atual = (cfg.get("contexto") or "").lower()

    # Toda troca que o usuário confirmou é um erro de transcrição que pode se
    # repetir: vira sugestão de correção, e o certo vira sugestão de termo.
    curtas = [s for s in substituicoes if len(s["de"].split()) <= 3]
    correcoes = _trocas(list(sug.get("correcoes") or []) + curtas)
    termos = [str(t).strip() for t in (sug.get("termos") or [])] + [s["para"] for s in curtas if len(s["para"].split()) <= 4]

    vistos, termos_ok = set(), []
    for t in termos:
        if t and t.lower() not in ja_termos and t.lower() not in vistos:
            vistos.add(t.lower())
            termos_ok.append(t)

    fatos = [
        str(f).strip() for f in (sug.get("contexto") or [])
        if str(f).strip()
        and str(f).strip().lower() not in contexto_atual
        and not re.match(r"(?i)^\s*(você|o usuário|eu)\b", str(f).strip())  # o "Sobre você" já cuida disso
    ]

    return {
        "resposta": str(dados.get("resposta") or "").strip() or "(sem resposta)",
        "substituicoes": substituicoes,
        "nova_ata": _titulos_markdown(str(dados.get("nova_ata") or "").strip()),
        "sugestoes_contexto": {
            "termos": termos_ok,
            "correcoes": [c for c in correcoes if c["de"].lower() not in ja_correcoes],
            "contexto": fatos,
        },
    }


def trocador(substituicoes: list[dict]):
    """Função que aplica as trocas num texto (palavra inteira, sem
    diferenciar maiúsculas — igual às Correções dos Ajustes)."""
    regras = [
        (re.compile(r"(?<!\w)" + re.escape(s["de"]) + r"(?!\w)", re.IGNORECASE), s["para"])
        for s in substituicoes
    ]
    return lambda texto: vocabulario.corrigir(texto, regras)


def acrescentar_linhas(atual: str, novas: list[str], chave=lambda l: l.strip().lower()) -> str:
    """Acrescenta linhas a um campo dos Ajustes sem repetir as que já existem."""
    existentes = {chave(l) for l in (atual or "").splitlines() if l.strip()}
    adicionar = []
    for linha in novas:
        if linha.strip() and chave(linha) not in existentes:
            existentes.add(chave(linha))
            adicionar.append(linha.strip())
    if not adicionar:
        return atual or ""
    base = (atual or "").rstrip()
    return (base + "\n" if base else "") + "\n".join(adicionar) + "\n"
