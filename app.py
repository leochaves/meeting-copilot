"""Abre o Copiloto de Reuniões numa janela própria.

    python app.py              # janela do app
    python app.py --navegador  # abre no navegador padrão em vez da janela
"""

import sys
from pathlib import Path

# Aberto pelo pythonw (Copiloto.bat) não existe console: sem isso, qualquer
# print/log/barra de progresso derruba o app em silêncio. Vai tudo pro log.
if sys.stdout is None or sys.stderr is None:
    _log_dir = Path(__file__).parent / "dados"
    _log_dir.mkdir(exist_ok=True)
    _log = open(_log_dir / "copiloto.log", "a", encoding="utf-8", buffering=1)
    sys.stdout = sys.stdout or _log
    sys.stderr = sys.stderr or _log

import threading  # noqa: E402
import time  # noqa: E402
import webbrowser  # noqa: E402

import uvicorn  # noqa: E402

from servidor import app  # noqa: E402

PORTA = 8765
URL = f"http://127.0.0.1:{PORTA}"
TITULO = "Copiloto de Reuniões"


def _focar_janela_existente() -> bool:
    """Se o app já está aberto, traz a janela dele pra frente."""
    if sys.platform != "win32":
        return False
    import ctypes

    user32 = ctypes.windll.user32
    janela = user32.FindWindowW(None, TITULO)
    if not janela:
        return False
    user32.ShowWindow(janela, 9)  # SW_RESTORE (desminimiza)
    user32.SetForegroundWindow(janela)
    return True


def _porta_ocupada() -> bool:
    import socket

    with socket.socket() as s:
        return s.connect_ex(("127.0.0.1", PORTA)) == 0


def main():
    if _porta_ocupada():
        if _focar_janela_existente():
            return
        if "--navegador" in sys.argv:
            webbrowser.open(URL)
            return
        sys.exit(f"A porta {PORTA} já está em uso por outro programa.")

    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")

    servidor = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=PORTA, log_level="warning"))
    thread = threading.Thread(target=servidor.run, daemon=True)
    thread.start()
    while not servidor.started:
        if not thread.is_alive():
            sys.exit(f"Não consegui abrir a porta {PORTA} — o app já está aberto?")
        time.sleep(0.05)

    if "--navegador" in sys.argv:
        webbrowser.open(URL)
        print(f"Copiloto rodando em {URL} — Ctrl+C pra sair")
        try:
            thread.join()
        except KeyboardInterrupt:
            pass
    else:
        import webview

        webview.create_window(TITULO, URL, width=1320, height=860, min_size=(960, 620))

        def trazer_pra_frente():
            # Aberto por atalho, o Windows às vezes deixa a janela atrás das
            # outras. Usa a API do Windows direto: mexer na janela via pywebview
            # a partir de um evento dele trava o app (deadlock).
            for _ in range(100):
                time.sleep(0.1)
                if _focar_janela_existente():
                    return

        threading.Thread(target=trazer_pra_frente, daemon=True).start()
        webview.start()

    servidor.should_exit = True
    thread.join(timeout=5)


if __name__ == "__main__":
    main()
