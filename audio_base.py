"""Parte da captura de áudio que é igual em todo sistema operacional.

Cada plataforma (audio_windows.py, audio_linux.py) só precisa alimentar as
duas fontes — `mic` (você) e `sistema` (os outros) — com amostras mono a
16 kHz; daqui saem os blocos que vão pro transcritor.
"""

import threading
import time

import numpy as np

TAXA = 16000
BLOCO_MS = 100
AMOSTRAS_BLOCO = TAXA * BLOCO_MS // 1000
MAX_ATRASO = TAXA  # 1 s de folga; acima disso descarta (deriva entre os relógios)


def para_mono_16k(dados: bytes, canais: int, taxa: int) -> np.ndarray:
    """PCM 16-bit intercalado em qualquer taxa → mono float32 a 16 kHz."""
    x = np.frombuffer(dados, dtype=np.int16).astype(np.float32)
    if canais > 1:
        x = x.reshape(-1, canais).mean(axis=1)
    if taxa != TAXA and len(x):
        n = int(round(len(x) * TAXA / taxa))
        x = np.interp(np.linspace(0, len(x) - 1, n), np.arange(len(x)), x)
    return x.astype(np.float32)


class Fonte:
    """Buffer de uma fonte. Alguns sistemas (o loopback do Windows) não
    entregam nada durante silêncio, então quem lê completa com zeros."""

    def __init__(self):
        self._buf = np.zeros(0, dtype=np.float32)
        self._lock = threading.Lock()

    def escrever(self, x: np.ndarray):
        with self._lock:
            self._buf = np.concatenate([self._buf, x])
            if len(self._buf) > MAX_ATRASO:
                self._buf = self._buf[-MAX_ATRASO:]

    def ler(self, n: int) -> np.ndarray:
        with self._lock:
            out, self._buf = self._buf[:n], self._buf[n:]
        if len(out) < n:
            out = np.concatenate([out, np.zeros(n - len(out), dtype=np.float32)])
        return out


class CapturadorBase:
    """Interface comum: `dev_mic`/`dev_sistema` ({"name": ...}), `iniciar()`,
    `blocos(parar)` e `parar()`."""

    dev_mic: dict | None = None
    dev_sistema: dict

    def __init__(self):
        self.mic = Fonte()
        self.sistema = Fonte()

    def iniciar(self):
        raise NotImplementedError

    def parar(self):
        raise NotImplementedError

    def blocos(self, parar: threading.Event):
        """Gera a cada 100 ms um bloco PCM 16-bit, 16 kHz, 2 canais
        intercalados (canal 0 = microfone, canal 1 = sistema)."""
        proximo = time.monotonic()
        while not parar.is_set():
            proximo += BLOCO_MS / 1000
            espera = proximo - time.monotonic()
            if espera > 0:
                time.sleep(espera)
            mic = self.mic.ler(AMOSTRAS_BLOCO)
            sis = self.sistema.ler(AMOSTRAS_BLOCO)
            estereo = np.empty(AMOSTRAS_BLOCO * 2, dtype=np.float32)
            estereo[0::2] = mic
            estereo[1::2] = sis
            yield np.clip(estereo, -32768, 32767).astype(np.int16).tobytes()
