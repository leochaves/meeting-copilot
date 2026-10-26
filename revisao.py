"""Revisão pós-reunião: o usuário conversa com a IA pra corrigir a transcrição
e a ata, e a IA aponta o que vale guardar no contexto (Ajustes) pras próximas.

A IA só propõe — nada muda até o usuário clicar em aplicar.
"""

import re
import unicodedata

import vocabulario
from sessao import limpar_ata, mmss

# --- tipo de pedido -------------------------------------------------------------
# Modelos pequenos se perdem quando um mesmo JSON tem que servir pra tudo
# (no teste, perguntas viravam "sugestões de contexto" e pedidos de mudar a
# ata também). Então o app decide o tipo e cada um tem uma chamada simples.

_PEDE_ATA = re.compile(
    r"\b(ata|resumo|decis\w*|tarefas?|pr[oó]ximos passos|pend[eê]ncias?|respons[aá]ve(l|is))\b", re.IGNORECASE
)
_VERBO_MUDAR = re.compile(
    r"\b(alter\w*|mud\w*|corrij\w*|corrig\w*|tir\w*|remov\w*|troqu\w*|troc\w*|inclu\w*|adicion\w*|"
    r"reescrev\w*|acrescent\w*|apag\w*|ajust\w*|coloqu\w*|ponha|p[oõ]e)\b", re.IGNORECASE
)
_PERGUNTA = re.compile(
    r"^\s*(o que|oq|que|quem|quando|como|qual|quais|onde|por ?que|quanto|quantos|teve|houve|algu[eé]m|"
    r"falaram|falou|lembra|me (diga|fala|explica)|explique|resuma)\b", re.IGNORECASE
)
_CORRECAO = re.compile(r"(=>|->|→|\bna verdade\b|\bn[aã]o [eé]\b|\bse escreve\b|\berrad\w*\b|\b[eé] o\b|\b[eé] a\b)", re.IGNORECASE)


def intencao(texto: str) -> str:
    """'ata' (mudar a ata), 'pergunta' (sobre a reunião) ou 'correcao'."""
    if _PEDE_ATA.search(texto) and _VERBO_MUDAR.search(texto):
        return "ata"
    if (texto.strip().endswith("?") or _PERGUNTA.search(texto)) and not _CORRECAO.search(texto):
        return "pergunta"
    return "correcao"


def _sem_acento(s: str) -> str:
    return "".join(c for c in unicodedata.normalize("NFD", s.lower()) if unicodedata.category(c) != "Mn")


_VAZIAS = set(
    "que quem qual quais como onde quando sobre algo alguma algum isso esse essa este esta para pra pro com "
    "uma umas uns dos das nos nas pelo pela foi ser era mais muito falou fala disse dito reuniao reunião "
    "tinha tem teve houve ele ela eles elas voce você nao não sim".split()
)


def palavras_chave(texto: str) -> list[str]:
    return [p for p in re.findall(r"\w{3,}", _sem_acento(texto)) if p not in _VAZIAS]


def trechos_relevantes(reuniao: dict, pergunta: str, max_chars: int | None, vizinhos: int = 2) -> str:
    """Falas que contêm as palavras da pergunta (sem acento, e também por
    prefixo, pra pegar 'tulio' em 'Túlio' e 'Tulho'), com as vizinhas pra dar
    contexto. Sem nenhuma, devolve a transcrição (cortada se preciso)."""
    falas = reuniao["falas"]
    chaves = palavras_chave(pergunta)
    linhas = [f"[{mmss(f['t'])}] {f['quem']}: {f['texto']}" for f in falas]
    pontos = []
    for i, f in enumerate(falas):
        palavras = set(re.findall(r"\w{3,}", _sem_acento(f["texto"])))
        acertos = sum(
            1 for c in chaves if c in palavras or any(p.startswith(c[:4]) for p in palavras if len(c) >= 4)
        )
        if acertos:
            pontos.append((acertos, i))
    if not pontos:
        texto = "\n".join(linhas)
        if max_chars and len(texto) > max_chars:
            texto = texto[: max_chars // 2] + "\n[…]\n" + texto[-max_chars // 2:]
        return texto

    escolhidas: set[int] = set()
    usados = 0
    for _acertos, i in sorted(pontos, reverse=True):  # as falas com mais palavras da pergunta primeiro
        bloco = {j for j in range(i - vizinhos, i + vizinhos + 1) if 0 <= j < len(falas)} - escolhidas
        custo = sum(len(linhas[j]) + 1 for j in bloco)
        if max_chars and usados + custo > max_chars:
            break
        escolhidas |= bloco
        usados += custo
    saida, anterior = [], None
    for j in sorted(escolhidas):
        if anterior is not None and j != anterior + 1:
            saida.append("[…]")
        saida.append(linhas[j])
        anterior = j
    return "\n".join(saida)


SISTEMA_PERGUNTA = """Você responde perguntas sobre uma reunião já encerrada, \
usando só os trechos da transcrição automática abaixo (pode ter erros de \
reconhecimento em nomes e termos — interprete pelo contexto; "Você" é o \
usuário). Responda em português do Brasil, direto, citando o horário [mm:ss] \
dos trechos em que se baseou. Se os trechos não falarem do assunto, diga que \
não encontrou isso na transcrição — não invente."""

SISTEMA_EDITAR_ATA = """Você edita a ata de uma reunião conforme o pedido do \
usuário. Aplique exatamente o que foi pedido e mantenha o resto igual. Use a \
transcrição só pra conferir nomes e fatos. Devolva somente a ata completa, em \
Markdown, com as mesmas seções `## ...` — sem comentários antes ou depois."""


def pedido_pergunta(reuniao: dict, cfg: dict, pergunta: str, max_chars: int | None) -> tuple[str, list[dict]]:
    # O contexto do usuário diz o que é cada sigla (ex.: "Solar" na fala = Solr)
    referencia = vocabulario.bloco_sistema(cfg.get("contexto", ""), vocabulario.termos(cfg.get("vocabulario", "")))
    sistema = SISTEMA_PERGUNTA + (
        "\n\nNão expanda siglas nem deduza o que um nome significa por conta própria: "
        "use o material de referência abaixo." + f"\n\n{referencia}" if referencia else ""
    )
    trechos = trechos_relevantes(reuniao, pergunta, max_chars)
    conteudo = f"Trechos da transcrição:\n\n{trechos}\n\n---\nPergunta: {pergunta}"
    return sistema, [{"role": "user", "content": conteudo}]


def pedido_editar_ata(reuniao: dict, pedido: str, max_chars: int | None) -> tuple[str, list[dict]]:
    # Pouca transcrição: modelos pequenos tendem a copiar o material de apoio pra resposta
    trechos = trechos_relevantes(reuniao, pedido, min(max_chars or 4000, 4000))
    conteudo = (
        f"<transcricao_para_conferir>\n{trechos}\n</transcricao_para_conferir>\n\n"
        f"<ata_atual>\n{sem_cercas(reuniao.get('ata') or '(sem ata)')}\n</ata_atual>\n\n"
        f"Pedido do usuário: {pedido}\n\n"
        "Devolva só a ata inteira já alterada, começando por `## Resumo`."
    )
    return SISTEMA_EDITAR_ATA, [{"role": "user", "content": conteudo}]


def sem_cercas(texto: str) -> str:
    """Tira ```markdown ... ``` que alguns modelos põem em volta da resposta."""
    return re.sub(r"^\s*```[\w-]*\s*\n|\n\s*```\s*$", "", (texto or "").strip()).strip()


# Se o modelo "ecoar" o material de apoio, corta a partir daí
_ECO = re.compile(r"(?im)^\s*(<\/?\w+>|trechos da transcri|pedido do usu|transcri[cç][aã]o pra conferir|---\s*$)")


def ata_editada(texto: str) -> str:
    """Limpa a resposta do modelo: cercas de código, linha 'Título:' e eco."""
    texto = sem_cercas(texto)
    eco = _ECO.search(texto)
    if eco:
        texto = texto[: eco.start()]
    linhas = texto.strip().splitlines()
    if linhas and linhas[0].lower().startswith("título:"):
        linhas = linhas[1:]
    return limpar_ata(_titulos_markdown("\n".join(linhas)))

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
        f"## Ata atual\n{sem_cercas(reuniao.get('ata') or '') or '(sem ata)'}",
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


# "Rafael: Refazer a carga..." é tarefa desta reunião, não fato pro contexto
_TAREFA = re.compile(r"^[^:]{2,60}:\s*\w+(ar|er|ir)\b", re.IGNORECASE)


def ja_sugerido(revisoes: list[dict]) -> dict:
    """O que a IA já sugeriu nesta revisão (modelos pequenos repetem tudo a
    cada resposta; o que o usuário não aceitou não deve voltar)."""
    vistos = {"termos": set(), "correcoes": set(), "contexto": set()}
    for r in revisoes:
        s = (r.get("dados") or {}).get("sugestoes_contexto") or {}
        vistos["termos"] |= {_sem_acento(t) for t in s.get("termos", [])}
        vistos["correcoes"] |= {_sem_acento(c["de"]) for c in s.get("correcoes", []) if isinstance(c, dict)}
        vistos["contexto"] |= {_sem_acento(f) for f in s.get("contexto", [])}
    return vistos


def normalizar(dados: dict, cfg: dict | None = None, vistos: dict | None = None) -> dict:
    """Garante o formato (modelos locais escorregam), completa as sugestões
    que dá pra deduzir e tira o que já está nos Ajustes ou já foi sugerido."""
    cfg = cfg or {}
    vistos = vistos or {"termos": set(), "correcoes": set(), "contexto": set()}
    sug = dados.get("sugestoes_contexto") or {}
    substituicoes = _trocas(dados.get("substituicoes"))

    ja_termos = {_sem_acento(t) for t in vocabulario.termos(cfg.get("vocabulario", ""))} | vistos["termos"]
    ja_correcoes = {
        _sem_acento(l.split("=>")[0].strip()) for l in (cfg.get("correcoes") or "").splitlines() if "=>" in l
    } | vistos["correcoes"]
    contexto_atual = _sem_acento(cfg.get("contexto") or "")

    # Toda troca que o usuário confirmou é um erro de transcrição que pode se
    # repetir: vira sugestão de correção, e o certo vira sugestão de termo.
    curtas = [s for s in substituicoes if len(s["de"].split()) <= 3]
    correcoes = _trocas(list(sug.get("correcoes") or []) + curtas)
    dos_usuario = {_sem_acento(s["para"]) for s in curtas}  # o certo das trocas sempre vale
    termos = [str(t).strip() for t in (sug.get("termos") or [])] + [s["para"] for s in curtas if len(s["para"].split()) <= 4]

    nesta, termos_ok = set(), []
    for t in termos:
        chave = _sem_acento(t)
        # Palavra comum em minúsculas ("espaço") não é nome nem sigla
        comum = t == t.lower() and not re.search(r"\d", t) and chave not in dos_usuario
        if t and not comum and chave not in ja_termos and chave not in nesta:
            nesta.add(chave)
            termos_ok.append(t)

    fatos = []
    for f in sug.get("contexto") or []:
        f = str(f).strip()
        chave = _sem_acento(f)
        if (
            f
            and chave not in contexto_atual
            and chave not in vistos["contexto"]
            and not re.match(r"(?i)^\s*(você|o usuário|eu)\b", f)  # o "Sobre você" já cuida disso
            and not _TAREFA.match(f)
        ):
            fatos.append(f)

    return {
        "resposta": str(dados.get("resposta") or "").strip() or "(sem resposta)",
        "substituicoes": substituicoes,
        "nova_ata": _titulos_markdown(str(dados.get("nova_ata") or "").strip()),
        "sugestoes_contexto": {
            "termos": termos_ok,
            "correcoes": [c for c in correcoes if _sem_acento(c["de"]) not in ja_correcoes],
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
