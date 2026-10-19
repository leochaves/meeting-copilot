"""Cérebro 100% local via Ollama (http://localhost:11434).

Modelos pequenos têm contexto curto, então em vez do histórico inteiro vai só
o system prompt + as últimas trocas (janela deslizante).
"""

import json
import os
import urllib.error
import urllib.request

from cerebro import SISTEMA, SISTEMA_ATA, com_contexto

MODELO_PADRAO = "gemma3:4b"
OLLAMA = os.getenv("OLLAMA_URL", "http://127.0.0.1:11434")  # IPv4 direto: localhost tenta IPv6 antes
JANELA = 8  # mensagens (4 trocas) mantidas no contexto
# Contexto pequeno = menos VRAM. Se a GPU transbordar, o Windows joga o excesso
# na RAM e a geração fica dezenas de vezes mais lenta.
OPCOES = {"num_ctx": int(os.getenv("OLLAMA_NUM_CTX", "4096")), "temperature": 0.4}
# Cabe no num_ctx junto com o prompt e a resposta (~3 caracteres por token)
MAX_CHARS_ATA = 7000

ERRO_OFFLINE = "Ollama não está rodando — abra o app Ollama ou rode `ollama serve`"


def _post(caminho: str, corpo: dict, timeout: float = 300):
    req = urllib.request.Request(
        OLLAMA + caminho,
        data=json.dumps(corpo).encode(),
        headers={"Content-Type": "application/json"},
    )
    return urllib.request.urlopen(req, timeout=timeout)


def listar_modelos() -> list[str]:
    try:
        with urllib.request.urlopen(OLLAMA + "/api/tags", timeout=2) as r:
            return sorted(m["name"] for m in json.load(r).get("models", []))
    except (urllib.error.URLError, OSError):
        return []


def modelos_carregados() -> list[dict]:
    try:
        with urllib.request.urlopen(OLLAMA + "/api/ps", timeout=1) as r:
            return [
                {"nome": m["name"], "vram": m.get("size_vram", 0), "total": m.get("size", 0)}
                for m in json.load(r).get("models", [])
            ]
    except (urllib.error.URLError, OSError):
        return []


class CerebroLocal:
    def __init__(self, modelo: str = MODELO_PADRAO, dispositivo: str = "gpu", contexto: str = ""):
        self.modelo = modelo
        self.mensagens: list[dict] = []
        self.sistema = com_contexto(SISTEMA, contexto)
        self.sistema_ata = com_contexto(SISTEMA_ATA, contexto)
        self._pensar = False  # desliga o "thinking" de modelos como qwen3; vira None se o modelo não aceitar
        # num_gpu 0 = nenhuma camada na placa de vídeo (bem mais lento: ~45 s
        # por explicação com gemma3:4b num Ryzen 7 2700)
        self.opcoes = {**OPCOES, "num_gpu": 0} if dispositivo == "cpu" else OPCOES

    def aquecer(self):
        """Carrega o modelo antes da reunião (a 1ª carga leva ~1 min)."""
        try:
            _post("/api/generate", {"model": self.modelo, "keep_alive": "30m", "options": self.opcoes}).read()
        except urllib.error.HTTPError as e:
            raise RuntimeError(f"Ollama recusou o modelo '{self.modelo}' ({e.code}) — já fez `ollama pull`?")
        except urllib.error.URLError:
            raise RuntimeError(ERRO_OFFLINE)

    def liberar(self):
        """Tira o modelo da GPU (o Ollama manteria por 30 min)."""
        try:
            _post("/api/generate", {"model": self.modelo, "keep_alive": 0}, timeout=10).read()
        except (urllib.error.URLError, OSError):
            pass

    def enviar(self, conteudo: str, ao_escrever) -> tuple[str, float]:
        self.mensagens.append({"role": "user", "content": conteudo})
        recentes = self.mensagens[-JANELA:]
        if recentes[0]["role"] != "user":
            recentes = recentes[1:]
        try:
            resposta = self._chat([{"role": "system", "content": self.sistema}] + recentes, ao_escrever)
        except Exception:
            self.mensagens.pop()
            raise
        self.mensagens.append({"role": "assistant", "content": resposta})
        return resposta, 0.0

    def gerar_ata(self, transcricao: str, explicacoes: str, ao_escrever) -> tuple[str, float]:
        # O contexto é curto: se a reunião não couber, usa as explicações
        # (que já resumem tudo) + o final da transcrição.
        if len(transcricao) > MAX_CHARS_ATA:
            resumo = explicacoes[-MAX_CHARS_ATA // 2:]
            final = transcricao[-MAX_CHARS_ATA // 2:]
            conteudo = (
                f"Resumos feitos durante a reunião:\n\n{resumo}\n\n"
                f"Final da transcrição:\n\n{final}"
            )
        else:
            conteudo = f"Transcrição:\n\n{transcricao}"
        mensagens = [
            {"role": "system", "content": self.sistema_ata},
            {"role": "user", "content": conteudo},
        ]
        return self._chat(mensagens, ao_escrever), 0.0

    def _chat(self, mensagens: list[dict], ao_escrever) -> str:
        corpo = {
            "model": self.modelo,
            "messages": mensagens,
            "stream": True,
            "keep_alive": "30m",
            "options": self.opcoes,
        }
        if self._pensar is not None:
            corpo["think"] = self._pensar
        try:
            return self._stream(corpo, ao_escrever)
        except urllib.error.HTTPError as e:
            if e.code == 400 and "think" in corpo:  # modelo sem suporte a thinking
                self._pensar = None
                del corpo["think"]
                return self._stream(corpo, ao_escrever)
            raise
        except urllib.error.URLError:
            raise RuntimeError(ERRO_OFFLINE)

    def _stream(self, corpo: dict, ao_escrever) -> str:
        partes = []
        with _post("/api/chat", corpo) as r:
            for linha in r:
                if not linha.strip():
                    continue
                msg = json.loads(linha)
                if "error" in msg:
                    raise RuntimeError(msg["error"])
                texto = msg.get("message", {}).get("content", "")
                if texto:
                    partes.append(texto)
                    ao_escrever(texto)
        return "".join(partes)
