"""Uso de recursos da máquina: GPU (NVIDIA), CPU, RAM e modelos no Ollama."""

import psutil

from cerebro_local import modelos_carregados

try:
    import pynvml

    pynvml.nvmlInit()
    _gpu = pynvml.nvmlDeviceGetHandleByIndex(0)
    _gpu_nome = pynvml.nvmlDeviceGetName(_gpu)
    if isinstance(_gpu_nome, bytes):
        _gpu_nome = _gpu_nome.decode()
except Exception:
    _gpu = None
    _gpu_nome = None

psutil.cpu_percent(None)  # a 1ª leitura sempre volta 0; descarta


def ler() -> dict:
    ram = psutil.virtual_memory()
    dados = {
        "cpu": psutil.cpu_percent(None),
        "ram_usada": ram.used,
        "ram_total": ram.total,
        "gpu": None,
        "ollama": modelos_carregados(),
    }
    if _gpu is not None:
        try:
            mem = pynvml.nvmlDeviceGetMemoryInfo(_gpu)
            uso = pynvml.nvmlDeviceGetUtilizationRates(_gpu)
            dados["gpu"] = {
                "nome": _gpu_nome,
                "vram_usada": mem.used,
                "vram_total": mem.total,
                "uso": uso.gpu,
            }
        except Exception:
            pass
    return dados
