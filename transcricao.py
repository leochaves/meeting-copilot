"""Transcrição ao vivo via WebSocket do Deepgram (multicanal + diarização)."""

import json
import os
import threading
from dataclasses import dataclass
from urllib.parse import urlencode

from websockets.sync.client import connect

from audio import TAXA


@dataclass
class Fala:
    t: float  # segundos desde o início da reunião
    quem: str
    texto: str


class Transcritor:
    def __init__(self, ao_receber, chave: str | None = None, termos: list[str] | None = None):
        self._ao_receber = ao_receber
        self._chave = chave or os.getenv("DEEPGRAM_API_KEY")
        if not self._chave:
            raise RuntimeError("Falta a chave do Deepgram (Ajustes → Transcrição)")
        self.descricao = "Deepgram (nuvem)"
        self.ultima_latencia: float | None = None
        params = {
            "model": os.getenv("DEEPGRAM_MODEL", "nova-3"),
            "language": os.getenv("DEEPGRAM_LANGUAGE", "pt-BR"),
            "encoding": "linear16",
            "sample_rate": TAXA,
            "channels": 2,
            "multichannel": "true",
            "diarize": "true",
            "punctuate": "true",
            "smart_format": "true",
            "endpointing": 400,
        }
        # Nomes/termos do usuário: "keyterm" no nova-3, "keywords" nos anteriores
        if termos:
            chave_termos = "keyterm" if params["model"].startswith("nova-3") else "keywords"
            params[chave_termos] = termos[:100]
        self._url = "wss://api.deepgram.com/v1/listen?" + urlencode(params, doseq=True)
        self._ws = None

    def rodar(self, blocos, parar: threading.Event):
        """Bloqueia até `parar` ser setado ou a conexão cair."""
        with connect(self._url, additional_headers={"Authorization": f"Token {self._chave}"}) as ws:
            self._ws = ws
            enviador = threading.Thread(target=self._enviar, args=(blocos, parar), daemon=True)
            enviador.start()
            try:
                for msg in ws:
                    self._tratar(json.loads(msg))
            finally:
                parar.set()

    def _enviar(self, blocos, parar):
        try:
            for bloco in blocos:
                self._ws.send(bloco)
            self._ws.send(json.dumps({"type": "CloseStream"}))
        except Exception:
            parar.set()

    def _tratar(self, msg: dict):
        if msg.get("type") != "Results" or not msg.get("is_final"):
            return
        alt = msg["channel"]["alternatives"][0]
        texto = alt.get("transcript", "").strip()
        if not texto:
            return
        canal = msg.get("channel_index", [0])[0]
        if canal == 0:
            quem = "Você"
        else:
            palavras = alt.get("words") or []
            spk = palavras[0].get("speaker") if palavras else None
            quem = f"Pessoa {spk + 1}" if spk is not None else "Outros"
        self._ao_receber(Fala(t=msg.get("start", 0.0), quem=quem, texto=texto))
