"""Captura no Windows: microfone + loopback WASAPI do dispositivo de saída.

O loopback pega tudo que sai pela saída (inclusive no fone), então funciona
com qualquer app de reunião.
"""

import pyaudiowpatch as pyaudio

from audio_base import BLOCO_MS, CapturadorBase, para_mono_16k


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


class Capturador(CapturadorBase):
    def __init__(self, usar_mic: bool = True, mic_index: int | None = None,
                 saida_index: int | None = None):
        super().__init__()
        self._pa = pyaudio.PyAudio()
        self._streams = []

        wasapi = self._pa.get_host_api_info_by_type(pyaudio.paWASAPI)
        self.dev_sistema = (
            self._pa.get_device_info_by_index(int(saida_index))
            if saida_index is not None
            else self._achar_loopback(wasapi)
        )
        self.dev_mic = None
        if usar_mic:
            idx = int(mic_index) if mic_index is not None else wasapi["defaultInputDevice"]
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

    def _abrir(self, dev: dict, fonte):
        canais = dev["maxInputChannels"]
        taxa = int(dev["defaultSampleRate"])

        def callback(dados, _frames, _info, _status):
            fonte.escrever(para_mono_16k(dados, canais, taxa))
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

    def parar(self):
        for s in self._streams:
            try:
                s.stop_stream()
                s.close()
            except OSError:
                pass
        self._pa.terminate()
