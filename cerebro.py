"""Conversa com o Claude: cada trecho da reunião vira uma mensagem nova.

O histórico só cresce (append-only), então o prompt caching reaproveita tudo
que já foi enviado e cada chamada paga só pelo trecho novo.
"""

import json

import anthropic

SISTEMA = """Você é um copiloto de reuniões ao vivo. Recebe trechos da \
transcrição automática de uma reunião (pode ter erros de reconhecimento — \
interprete pelo contexto) e ajuda o usuário, identificado como "Você", a \
acompanhar e participar.

A cada trecho, responda em português do Brasil, curto, pra ser lido de relance \
durante a reunião, neste formato:

📌 Agora: 1–2 frases sobre o que está sendo discutido.
🧭 Contexto: termos, siglas, subtexto ou ligação com o que veio antes — só se \
ajudar a entender.
💡 Sugestões: 1–3 itens do que o usuário poderia dizer, perguntar ou observar.
⚠️ Atenção: decisões, prazos ou tarefas atribuídas ao usuário — só se houver.

Omita seções vazias. Fale só do que foi dito nos trechos — nunca complete com \
suposições. Se o trecho for irrelevante ou sem sentido (conversa paralela, \
teste de microfone, silêncio, ruído), diga isso em uma linha.

Quando o usuário fizer uma pergunta direta, responda a pergunta usando tudo o \
que foi dito na reunião até ali, sem o formato acima."""

SISTEMA_ATA = """Você recebe a transcrição automática de uma reunião (pode ter \
erros de reconhecimento — interprete pelo contexto; "Você" é o usuário). \
Escreva a ata em Markdown, em português do Brasil.

A primeira linha deve ser exatamente `Título: <título curto, até 8 palavras>`. \
Depois, use estas seções (omita as vazias):

## Resumo
3–5 frases.
## Decisões
## Tarefas
Quem, o quê e prazo, quando tiver sido dito.
## Pendências
Dúvidas e assuntos sem conclusão.
## Próximos passos

Regras:
- Use exclusivamente o que foi dito na transcrição. Não invente assuntos, \
nomes, decisões, tarefas, números ou prazos.
- Cada decisão e cada tarefa precisa ter sido dita de fato. Uma proposta ou \
sugestão não é decisão.
- Se a transcrição for curta, sem sentido ou não parecer uma reunião de \
trabalho (teste de microfone, frases soltas, ruído), não force uma ata: depois \
do título, escreva só `## Resumo` e uma frase dizendo isso."""

LEMBRETE_ATA = (
    "\n\n---\nEscreva a ata usando somente o que está na transcrição acima. "
    "O material de referência das instruções serve só pra grafia de nomes e "
    "termos — não é conteúdo desta reunião."
)

MODELO_PADRAO = "claude-opus-5-5"

# US$ por milhão de tokens: (entrada, saída, leitura de cache).
# Escrita de cache custa 1,25× a entrada.
PRECOS = {
    "claude-fable-5-1": (10.0, 50.0, 0.25),
    "claude-opus-5-5": (4.0, 20.0, 0.20),
    "claude-sonnet-5-5": (2.0, 10.0, 0.20),
    "claude-haiku-5-5": (0.10, 0.50, 0.01),
}


def _custo(modelo: str, uso) -> float:
    entrada, saida, cache = PRECOS.get(modelo, PRECOS[MODELO_PADRAO])
    escrita = getattr(uso, "cache_creation_input_tokens", 0) or 0
    leitura = getattr(uso, "cache_read_input_tokens", 0) or 0
    return (
        uso.input_tokens * entrada
        + escrita * entrada * 1.25
        + leitura * cache
        + uso.output_tokens * saida
    ) / 1e6


def com_contexto(sistema: str, extra: str) -> str:
    """Acrescenta o contexto do usuário (vocabulário etc.) ao system prompt."""
    return f"{sistema}\n\n{extra}" if extra else sistema


class Cerebro:
    def __init__(self, modelo: str = MODELO_PADRAO, esforco: str = "low",
                 api_key: str | None = None, contexto: str = ""):
        self.client = anthropic.Anthropic(api_key=api_key) if api_key else anthropic.Anthropic()
        self.modelo = modelo
        self.esforco = esforco
        self.mensagens: list[dict] = []
        # Fixo durante a reunião, então o prompt caching continua valendo
        self.sistema = com_contexto(SISTEMA, contexto)
        self.sistema_ata = com_contexto(SISTEMA_ATA, contexto)

    def _stream(self, ao_escrever, esquema: dict | None = None, max_tokens: int = 8000, **kwargs):
        config_saida = {"effort": self.esforco}
        if esquema:  # saída estruturada: a resposta vem como JSON válido pro esquema
            config_saida["format"] = {"type": "json_schema", "schema": esquema}
        kwargs.update(model=self.modelo, max_tokens=max_tokens, output_config=config_saida)
        # Haiku não tem fallback server-side; nos demais, se o classificador
        # recusar, o próprio servidor refaz com o modelo recomendado.
        if "haiku" not in self.modelo:
            kwargs["betas"] = ["server-side-fallback-2026-07-01"]
            kwargs["extra_body"] = {"fallbacks": "default"}
        with self.client.beta.messages.stream(**kwargs) as stream:
            for texto in stream.text_stream:
                ao_escrever(texto)
            return stream.get_final_message()

    def enviar(self, conteudo: str, ao_escrever) -> tuple[str, float]:
        """Manda `conteudo` como nova mensagem; devolve (texto, custo em US$)."""
        self.mensagens.append({"role": "user", "content": conteudo})
        try:
            final = self._stream(
                ao_escrever,
                system=self.sistema,
                messages=self.mensagens,
                cache_control={"type": "ephemeral"},
            )
        except Exception:
            self.mensagens.pop()
            raise

        custo = _custo(self.modelo, final.usage)
        if final.stop_reason == "refusal":
            self.mensagens.pop()
            return "[o modelo recusou esse trecho]", custo

        # Devolve o content inteiro (inclui blocos de thinking) — exigido pra
        # continuar a conversa no mesmo modelo.
        self.mensagens.append({"role": "assistant", "content": final.content})
        return "".join(b.text for b in final.content if b.type == "text"), custo

    def completar(self, sistema: str, mensagens: list[dict]) -> tuple[str, float]:
        """Resposta em texto livre (perguntas sobre a reunião, editar a ata)."""
        final = self._stream(lambda _t: None, max_tokens=16000, system=sistema, messages=mensagens)
        custo = _custo(self.modelo, final.usage)
        if final.stop_reason == "refusal":
            return "[o modelo recusou esse pedido]", custo
        return "".join(b.text for b in final.content if b.type == "text"), custo

    def revisar(self, sistema: str, mensagens: list[dict], esquema: dict) -> tuple[dict, float]:
        """Revisão pós-reunião: devolve (JSON no formato do esquema, custo)."""
        # max_tokens maior: a resposta pode trazer a ata inteira reescrita
        final = self._stream(lambda _t: None, esquema=esquema, max_tokens=16000,
                             system=sistema, messages=mensagens)
        custo = _custo(self.modelo, final.usage)
        if final.stop_reason == "refusal":
            return {"resposta": "[o modelo recusou esse pedido]"}, custo
        if final.stop_reason == "max_tokens":
            return {"resposta": "[a resposta ficou grande demais; peça uma correção de cada vez]"}, custo
        texto = "".join(b.text for b in final.content if b.type == "text")
        return json.loads(texto), custo

    def gerar_ata(self, transcricao: str, _explicacoes: str, ao_escrever) -> tuple[str, float]:
        final = self._stream(
            ao_escrever,
            system=self.sistema_ata,
            messages=[{"role": "user", "content": f"Transcrição:\n\n{transcricao}{LEMBRETE_ATA}"}],
        )
        texto = "".join(b.text for b in final.content if b.type == "text")
        return texto, _custo(self.modelo, final.usage)
