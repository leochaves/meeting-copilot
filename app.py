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
        # Já aberto: no Windows traz a janela pra frente; no Linux abre no navegador
        if not _focar_janela_existente():
            webbrowser.open(URL)
        return

    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")

    servidor = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=PORTA, log_level="warning"))
    thread = threading.Thread(target=servidor.run, daemon=True)
    thread.start()
    while not servidor.started:
        if not thread.is_alive():
            sys.exit(f"Não consegui abrir a porta {PORTA} — o app já está aberto?")
        time.sleep(0.05)

    if "--navegador" in sys.argv or not _abrir_janela():
        webbrowser.open(URL)
        print(f"Copiloto rodando em {URL} — Ctrl+C pra sair")
        try:
            thread.join()
        except KeyboardInterrupt:
            pass

    servidor.should_exit = True
    thread.join(timeout=5)


def _qt_funciona() -> bool:
    """No Linux, se faltar alguma biblioteca do sistema o Qt aborta o processo
    inteiro (sem exceção pra tratar). Testa num processo separado antes."""
    import subprocess

    teste = "from PyQt6.QtWidgets import QApplication; QApplication([])"
    try:
        r = subprocess.run([sys.executable, "-c", teste], capture_output=True, text=True, timeout=20)
    except (OSError, subprocess.TimeoutExpired) as e:
        print(f"[teste do Qt falhou: {e}]")
        return False
    if r.returncode != 0:
        print(f"[Qt indisponível — rode o instalar.sh de novo pra instalar as bibliotecas]\n{r.stderr[-800:]}")
        return False
    return True


def _abrir_janela() -> bool:
    """Janela própria (WebView2 no Windows, Qt/GTK no Linux). Bloqueia até
    fechar; devolve False se não houver motor de janela disponível."""
    if sys.platform.startswith("linux") and not _qt_funciona():
        return False
    try:
        import webview

        # text_select: o pywebview bloqueia a seleção de texto por padrão
        webview.create_window(TITULO, URL, width=1320, height=860, min_size=(960, 620), text_select=True)
    except Exception as e:
        print(f"[sem janela própria ({e}); abrindo no navegador]")
        return False

    def trazer_pra_frente():
        # Aberto por atalho, o Windows às vezes deixa a janela atrás das
        # outras. Usa a API do Windows direto: mexer na janela via pywebview
        # a partir de um evento dele trava o app (deadlock).
        for _ in range(100):
            time.sleep(0.1)
            if _focar_janela_existente():
                return

    if sys.platform == "win32":
        threading.Thread(target=trazer_pra_frente, daemon=True).start()
    try:
        # No Linux, Qt direto: é o que o instalador prepara (sem tentar o GTK antes)
        webview.start(gui="qt" if sys.platform.startswith("linux") else None)
    except Exception as e:
        print(f"[a janela não abriu ({e}); abrindo no navegador]")
        return False
    return True


if __name__ == "__main__":
    main()
