"""Captura de áudio no Windows: microfone (você) + loopback do sistema (os outros).

Funciona com qualquer app de reunião (Meet, Zoom, Teams...), porque o loopback
pega tudo que sai pelo dispositivo de saída — inclusive no fone.

Saída: blocos de 100 ms em PCM 16-bit, 16 kHz, 2 canais intercalados
(canal 0 = microfone, canal 1 = sistema).
"""

import threading
import time

import numpy as np
import pyaudiowpatch as pyaudio

TAXA = 16000
BLOCO_MS = 100
AMOSTRAS_BLOCO = TAXA * BLOCO_MS // 1000
MAX_ATRASO = TAXA  # 1 s de folga; acima disso descarta (deriva entre os relógios)


def listar_dispositivos() -> dict:
    """Microfones e saídas (loopback) disponíveis no WASAPI."""
    pa = pyaudio.PyAudio()
    try:
        wasapi = pa.get_host_api_info_by_type(pyaudio.paWASAPI)
        mics, saidas = [], []
        for i in range(wasapi["deviceCount"]):
            d = pa.get_device_info_by_host_api_device_index(wasapi["index"], i)
            item = {"index": d["index"], "nome": d["name"]}
            if d.get("isLoopbackDevice"):
                item["nome"] = item["nome"].replace(" [Loopback]", "")
                saidas.append(item)
            elif d["maxInputChannels"] > 0:
                mics.append(item)
        return {"mics": mics, "saidas": saidas}
    finally:
        pa.terminate()


def _para_mono_16k(dados: bytes, canais: int, taxa: int) -> np.ndarray:
    x = np.frombuffer(dados, dtype=np.int16).astype(np.float32)
    if canais > 1:
        x = x.reshape(-1, canais).mean(axis=1)
    if taxa != TAXA and len(x):
        n = int(round(len(x) * TAXA / taxa))
        x = np.interp(np.linspace(0, len(x) - 1, n), np.arange(len(x)), x)
    return x.astype(np.float32)


class _Fonte:
    """Buffer de uma fonte. O loopback do WASAPI não entrega nada durante
    silêncio, então quem lê completa com zeros em vez de esperar."""

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


class Capturador:
    def __init__(self, usar_mic: bool = True, mic_index: int | None = None,
                 saida_index: int | None = None):
        self._pa = pyaudio.PyAudio()
        self._streams = []
        self.mic = _Fonte()
        self.sistema = _Fonte()

        wasapi = self._pa.get_host_api_info_by_type(pyaudio.paWASAPI)
        self.dev_sistema = (
            self._pa.get_device_info_by_index(saida_index)
            if saida_index is not None
            else self._achar_loopback(wasapi)
        )
        self.dev_mic = None
        if usar_mic:
            idx = mic_index if mic_index is not None else wasapi["defaultInputDevice"]
            self.dev_mic = self._pa.get_device_info_by_index(idx)

    def _achar_loopback(self, wasapi) -> dict:
        saida = self._pa.get_device_info_by_index(wasapi["defaultOutputDevice"])
        if saida.get("isLoopbackDevice"):
            return saida
        for lb in self._pa.get_loopback_device_info_generator():
            if saida["name"] in lb["name"]:
                return lb
        raise RuntimeError(
            f"Não achei o loopback de '{saida['name']}'. "
            "Confira o dispositivo de saída padrão do Windows."
        )

    def _abrir(self, dev: dict, fonte: _Fonte):
        canais = dev["maxInputChannels"]
        taxa = int(dev["defaultSampleRate"])

        def callback(dados, _frames, _info, _status):
            fonte.escrever(_para_mono_16k(dados, canais, taxa))
            return (None, pyaudio.paContinue)

        stream = self._pa.open(
            format=pyaudio.paInt16,
            channels=canais,
            rate=taxa,
            input=True,
            input_device_index=dev["index"],
            frames_per_buffer=int(taxa * BLOCO_MS / 1000),
            stream_callback=callback,
        )
        self._streams.append(stream)

    def iniciar(self):
        self._abrir(self.dev_sistema, self.sistema)
        if self.dev_mic:
            self._abrir(self.dev_mic, self.mic)

    def blocos(self, parar: threading.Event):
        """Gera um bloco estéreo a cada 100 ms, no ritmo do relógio."""
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

    def parar(self):
        for s in self._streams:
            try:
                s.stop_stream()
                s.close()
            except OSError:
                pass
        self._pa.terminate()
