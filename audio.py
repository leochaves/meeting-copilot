"""Captura de áudio: microfone (você) + áudio do computador (os outros).

Escolhe a implementação do sistema operacional; o resto do app só usa
`Capturador`, `listar_dispositivos` e as constantes daqui.
"""

import sys

from audio_base import AMOSTRAS_BLOCO, BLOCO_MS, TAXA  # noqa: F401

if sys.platform == "win32":
    from audio_windows import Capturador, listar_dispositivos  # noqa: F401
elif sys.platform.startswith("linux"):
    from audio_linux import Capturador, listar_dispositivos  # noqa: F401
else:
    raise RuntimeError(f"Sistema não suportado: {sys.platform} (só Windows e Linux)")
