"""Transcrição local e grátis com faster-whisper (GPU NVIDIA, ou CPU como reserva).

O Whisper não é streaming: o áudio de cada canal é cortado em frases pelas
pausas (detector simples de energia) e cada frase vai pro modelo. Dá alguns
segundos de atraso — irrelevante pra quem explica de minuto em minuto.

Sem diarização: tudo que vem do sistema aparece como "Outros".
"""

import os
import queue
import threading
import time
from pathlib import Path

import numpy as np

from audio import AMOSTRAS_BLOCO, BLOCO_MS, TAXA
from transcricao import Fala

PAUSA_FIM = 0.7  # s de silêncio que fecham uma frase
MAX_FRASE = 20.0  # s — corta frases longas pra não atrasar demais
MIN_FRASE = 0.4  # s — ignora estalos

# Alucinações clássicas do Whisper em ruído/silêncio
LIXO = ("legendas pela comunidade", "amara.org", "inscreva-se", "obrigado por assistir")

_modelos: dict = {}
_lock_carga = threading.Lock()


def _registrar_dlls_cuda():
    """As bibliotecas do CUDA instaladas via pip (nvidia-cublas/cudnn) ficam
    dentro do site-packages, fora do caminho de busca do sistema."""
    import importlib.util

    spec = importlib.util.find_spec("nvidia")
    if not spec or not spec.submodule_search_locations:
        return
    for base in spec.submodule_search_locations:
        if os.name == "nt":
            for pasta in Path(base).glob("*/bin"):
                if str(pasta) not in os.environ["PATH"]:
                    os.add_dll_directory(str(pasta))
                    os.environ["PATH"] = str(pasta) + os.pathsep + os.environ["PATH"]
        else:
            # No Linux o LD_LIBRARY_PATH só vale se definido antes do processo
            # abrir; então pré-carrega os .so com RTLD_GLOBAL. Quando o
            # ctranslate2 pedir libcublas/libcudnn pelo nome, já estão na memória.
            import ctypes

            for padrao in ("cublas/lib/libcublasLt.so.*", "cublas/lib/libcublas.so.*",
                           "cudnn/lib/libcudnn*.so.*"):
                for so in sorted(Path(base).glob(padrao)):
                    try:
                        ctypes.CDLL(str(so), mode=ctypes.RTLD_GLOBAL)
                    except OSError:
                        pass


def _modelo_cpu(nome: str):
    import psutil
    from faster_whisper import WhisperModel

    nucleos = psutil.cpu_count(logical=False) or 4
    return WhisperModel(nome, device="cpu", compute_type="int8", cpu_threads=nucleos), f"{nome} (CPU)"


def carregar_modelo(nome: str = "large-v3-turbo", dispositivo: str = "gpu"):
    """Carrega (uma vez só enquanto não for liberado) e devolve (modelo, descrição).

    Na CPU fica bem mais lento: num Ryzen 7 2700, o `small` leva ~6 s por
    frase de 4,5 s — use com modelos pequenos.
    """
    with _lock_carga:
        chave = (nome, dispositivo)
        if chave in _modelos:
            return _modelos[chave]
        if dispositivo == "cpu":
            r = _modelo_cpu(nome)
        else:
            _registrar_dlls_cuda()
            from faster_whisper import WhisperModel

            # int8_float16 usa ~metade da VRAM do float16, com qualidade quase
            # igual — importante quando a GPU também roda o modelo do Ollama.
            precisao = os.getenv("WHISPER_COMPUTE", "int8_float16")
            try:
                r = WhisperModel(nome, device="cuda", compute_type=precisao), f"{nome} (GPU)"
            except Exception as e:
                print(f"[GPU indisponível pro Whisper ({e}); usando small na CPU]")
                r = _modelo_cpu("small")
        _modelos[chave] = r
        return r


def liberar_modelos():
    """Solta o Whisper da GPU (volta a carregar na próxima reunião)."""
    import gc

    with _lock_carga:
        _modelos.clear()
    gc.collect()


class _Cortador:
    """Junta blocos de um canal e devolve uma frase quando ela termina."""

    def __init__(self, limiar: float):
        self.limiar = limiar
        self.blocos: list[np.ndarray] = []
        self.inicio = 0.0
        self.silencio = 0.0
        self.falando = False

    def adicionar(self, x: np.ndarray, t: float):
        dur = BLOCO_MS / 1000
        voz = np.sqrt(np.mean(x**2)) > self.limiar
        if not self.falando:
            if voz:
                self.falando, self.inicio, self.silencio = True, t, 0.0
                self.blocos = [x]
            return None
        self.blocos.append(x)
        self.silencio = 0.0 if voz else self.silencio + dur
        duracao = len(self.blocos) * dur
        if self.silencio >= PAUSA_FIM or duracao >= MAX_FRASE:
            return self.fechar()
        return None

    def fechar(self):
        if not self.falando:
            return None
        duracao = len(self.blocos) * BLOCO_MS / 1000
        audio, inicio = np.concatenate(self.blocos), self.inicio
        self.blocos, self.falando = [], False
        if duracao - self.silencio < MIN_FRASE:
            return None
        return inicio, audio


class TranscritorWhisper:
    def __init__(self, ao_receber, modelo: str = "large-v3-turbo", limiar: float = 250,
                 idioma: str = "pt", dispositivo: str = "gpu"):
        self._ao_receber = ao_receber
        self._fila: queue.Queue = queue.Queue()
        self._modelo, self.descricao = carregar_modelo(modelo, dispositivo)
        self._idioma = idioma
        self._limiar = limiar
        self.ultima_latencia: float | None = None  # s entre o fim da frase e o texto pronto

    def liberar(self):
        self._modelo = None
        liberar_modelos()

    def rodar(self, blocos, parar: threading.Event):
        trabalhador = threading.Thread(target=self._transcrever, daemon=True)
        trabalhador.start()
        cortadores = {"Você": _Cortador(self._limiar), "Outros": _Cortador(self._limiar)}
        t = 0.0
        for bloco in blocos:
            estereo = np.frombuffer(bloco, dtype=np.int16).astype(np.float32).reshape(-1, 2)
            for canal, quem in enumerate(cortadores):
                frase = cortadores[quem].adicionar(estereo[:, canal], t)
                if frase:
                    self._fila.put((quem, *frase, time.monotonic()))
            t += AMOSTRAS_BLOCO / TAXA
        # Encerrou: transcreve o que ficou pela metade e espera a fila esvaziar
        for quem, c in cortadores.items():
            frase = c.fechar()
            if frase:
                self._fila.put((quem, *frase, time.monotonic()))
        self._fila.put(None)
        trabalhador.join()

    def _transcrever(self):
        while True:
            item = self._fila.get()
            if item is None:
                return
            quem, inicio, audio, enfileirado = item
            try:
                segmentos, _ = self._modelo.transcribe(
                    (audio / 32768.0).astype(np.float32),
                    language=self._idioma,
                    vad_filter=True,
                    condition_on_previous_text=False,
                    beam_size=5,
                )
                texto = " ".join(s.text.strip() for s in segmentos).strip()
            except Exception as e:
                print(f"[erro no Whisper: {e}]")
                continue
            self.ultima_latencia = time.monotonic() - enfileirado
            if texto and not any(l in texto.lower() for l in LIXO):
                self._ao_receber(Fala(t=inicio, quem=quem, texto=texto))
