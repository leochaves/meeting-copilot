"""Captura no Linux: microfone + "monitor" da saída (PulseAudio ou PipeWire).

Todo dispositivo de saída tem uma fonte "Monitor of ..." que entrega o que
está tocando nele — o equivalente ao loopback do Windows. Funciona no
PipeWire através do pipewire-pulse (padrão no Ubuntu 22.10+, Fedora etc.).
"""

import threading

from audio_base import AMOSTRAS_BLOCO, TAXA, CapturadorBase


def _soundcard():
    """Importa sob demanda: o soundcard se conecta ao PulseAudio já no import,
    e sem servidor de som o app inteiro deixaria de abrir."""
    try:
        import soundcard
    except Exception as e:
        raise RuntimeError(
            "Não consegui falar com o servidor de som (PulseAudio/PipeWire). "
            f"Ele está rodando nesta sessão? Detalhe: {type(e).__name__} {e}"
        ) from e
    return soundcard


def listar_dispositivos() -> dict:
    sc = _soundcard()
    mics, saidas = [], []
    for m in sc.all_microphones(include_loopback=True):
        item = {"index": m.id, "nome": m.name}
        if m.isloopback:
            item["nome"] = item["nome"].removeprefix("Monitor of ")
            saidas.append(item)
        else:
            mics.append(item)
    return {"mics": mics, "saidas": saidas}


def _monitor_padrao(sc):
    alto_falante = sc.default_speaker()
    # O id do monitor é "<id da saída>.monitor"; get_microphone casa por trecho do id/nome
    for chave in (alto_falante.id, alto_falante.name):
        try:
            return sc.get_microphone(chave, include_loopback=True)
        except IndexError:
            continue
    raise RuntimeError(
        f"Não achei o monitor da saída '{alto_falante.name}'. "
        "Confira se o PulseAudio/PipeWire está rodando."
    )


class Capturador(CapturadorBase):
    def __init__(self, usar_mic: bool = True, mic_index: str | None = None,
                 saida_index: str | None = None):
        super().__init__()
        sc = _soundcard()
        self._sistema = (
            sc.get_microphone(saida_index, include_loopback=True)
            if saida_index is not None
            else _monitor_padrao(sc)
        )
        self.dev_sistema = {"name": self._sistema.name.removeprefix("Monitor of ")}
        self._mic = None
        if usar_mic:
            self._mic = sc.get_microphone(mic_index) if mic_index is not None else sc.default_microphone()
            self.dev_mic = {"name": self._mic.name}
        self._fim = threading.Event()
        self._threads: list[threading.Thread] = []

    def _ler(self, dispositivo, fonte):
        try:
            # O servidor de som já entrega mono a 16 kHz (ele converte)
            with dispositivo.recorder(samplerate=TAXA, channels=1, blocksize=AMOSTRAS_BLOCO) as rec:
                while not self._fim.is_set():
                    dados = rec.record(numframes=AMOSTRAS_BLOCO)  # float32 em [-1, 1]
                    fonte.escrever(dados[:, 0] * 32767.0)
        except Exception as e:
            print(f"[captura de '{dispositivo.name}' parou: {e}]")

    def iniciar(self):
        pares = [(self._sistema, self.sistema)]
        if self._mic:
            pares.append((self._mic, self.mic))
        for dispositivo, fonte in pares:
            t = threading.Thread(target=self._ler, args=(dispositivo, fonte), daemon=True)
            t.start()
            self._threads.append(t)

    def parar(self):
        self._fim.set()
        for t in self._threads:
            t.join(timeout=2)
