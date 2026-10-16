#!/usr/bin/env bash
# Instalador do Copiloto de Reuniões no Linux (Ubuntu/Debian, Fedora, Arch).
#
# Pode rodar de novo quando quiser: o que já estiver instalado é pulado.
#
#   ./instalar.sh                  # pergunta se quer o modo local (Ollama)
#   ./instalar.sh --com-ollama     # instala o modo local sem perguntar
#   ./instalar.sh --sem-ollama     # só Claude
#   ./instalar.sh --sem-atalho     # não cria o atalho no menu
#   ./instalar.sh --modelo-local qwen3:4b
set -euo pipefail

RAIZ="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$RAIZ"
PY="$RAIZ/.venv/bin/python"

OLLAMA_OPCAO=""
SEM_ATALHO=""
MODELO_LOCAL="gemma3:4b"
while [ $# -gt 0 ]; do
    case "$1" in
        --com-ollama) OLLAMA_OPCAO=sim ;;
        --sem-ollama) OLLAMA_OPCAO=nao ;;
        --sem-atalho) SEM_ATALHO=1 ;;
        --modelo-local) MODELO_LOCAL="$2"; shift ;;
        *) echo "Opção desconhecida: $1"; exit 1 ;;
    esac
    shift
done

passo() { printf '\n\033[36m==> %s\033[0m\n' "$1"; }
ok() { printf '    \033[32mOK\033[0m  %s\n' "$1"; }
aviso() { printf '    \033[33m!\033[0m   %s\n' "$1"; }
falha() { printf '\n\033[31mERRO: %s\033[0m\n' "$1" >&2; exit 1; }

SUDO=""
[ "$(id -u)" -ne 0 ] && SUDO="sudo"

if command -v apt-get >/dev/null 2>&1; then PM=apt
elif command -v dnf >/dev/null 2>&1; then PM=dnf
elif command -v pacman >/dev/null 2>&1; then PM=pacman
else PM=""
fi

# instalar_pacotes "<apt>" "<dnf>" "<pacman>"
instalar_pacotes() {
    case "$PM" in
        apt) $SUDO apt-get update -qq && $SUDO env DEBIAN_FRONTEND=noninteractive apt-get install -y -qq --no-install-recommends $1 ;;
        # strict=False: pula pacotes que não existem nessa versão (ex.: RHEL/Oracle 8)
        # em vez de cancelar a instalação inteira
        dnf) $SUDO dnf install -y -q --setopt=strict=False $2 ;;
        pacman) $SUDO pacman -S --needed --noconfirm $3 ;;
        *) aviso "Gerenciador de pacotes não reconhecido; instale manualmente o equivalente a: $1"; return 0 ;;
    esac
}

salvar_config() {
    # Grava um padrão nos Ajustes do app (banco local em dados/)
    COPILOTO_CFG="$1" "$PY" -c "import json, os, db; db.Db().salvar_config(json.loads(os.environ['COPILOTO_CFG']))"
}

# --- 1. Sistema ------------------------------------------------------------------
passo "Verificando o sistema"
[ "$(uname -s)" = "Linux" ] || falha "Este instalador é pro Linux. No Windows, use Instalar.bat."
[ "$(uname -m)" = "x86_64" ] || aviso "Arquitetura $(uname -m): só x86_64 foi testada."
livre=$(df -BG --output=avail "$RAIZ" | tail -1 | tr -dc '0-9')
if [ "${livre:-0}" -lt 10 ]; then aviso "Só ${livre} GB livres; a instalação completa usa ~10 GB."; else ok "${livre} GB livres"; fi
[ -n "$PM" ] && ok "gerenciador de pacotes: $PM"
[ -r /etc/os-release ] && ok "$(. /etc/os-release && echo "$PRETTY_NAME")"
# O app precisa rodar na máquina onde a reunião toca: com tela e som
if [ -z "${DISPLAY:-}${WAYLAND_DISPLAY:-}" ]; then
    aviso "Nenhuma tela gráfica detectada (sessão por SSH?). O app precisa rodar no computador onde a reunião toca."
fi
if command -v pactl >/dev/null 2>&1 && ! pactl info >/dev/null 2>&1; then
    aviso "Servidor de som (PulseAudio/PipeWire) não respondeu: sem ele não dá pra capturar o áudio."
fi

# --- 2. Pacotes do sistema --------------------------------------------------------
passo "Pacotes do sistema (pode pedir sua senha pro sudo)"
instalar_pacotes \
    "python3 python3-venv python3-pip libpulse0 pulseaudio-utils curl xdg-utils libnss3 libxkbcommon-x11-0 libegl1 libgl1 libxcomposite1 libxdamage1 libxrandr2 libxtst6 libxcb-cursor0 libxcb-icccm4 libxcb-keysyms1 libxcb-image0 libxcb-render-util0 libxcb-shape0 libxcb-xinerama0 libxcb-xkb1 libxcb-randr0 fonts-noto-color-emoji" \
    "pulseaudio-libs pulseaudio-utils curl xdg-utils nss libxkbcommon-x11 mesa-libEGL mesa-libGL xcb-util-cursor xcb-util-wm xcb-util-keysyms xcb-util-image xcb-util-renderutil google-noto-emoji-color-fonts" \
    "python python-pip libpulse curl xdg-utils nss libxkbcommon-x11 mesa xcb-util-cursor xcb-util-wm xcb-util-keysyms xcb-util-image xcb-util-renderutil noto-fonts-emoji"
ok "pacotes do sistema"

# --- 3. Python --------------------------------------------------------------------
passo "Python 3.10 a 3.13"
# Distribuições antigas (RHEL/Oracle 8, Ubuntu 20.04) têm python3 = 3.6/3.8,
# mas oferecem versões novas com nome próprio (python3.12 etc.)
achar_python() {
    for p in python3.12 python3.11 python3.13 python3.10 python3; do
        command -v "$p" >/dev/null 2>&1 || continue
        menor=$("$p" -c 'import sys; print(sys.version_info[1] if sys.version_info[0] == 3 else 0)' 2>/dev/null) || continue
        if [ "$menor" -ge 10 ] && [ "$menor" -le 13 ]; then echo "$p"; return 0; fi
    done
    return 1
}
if [ -x "$PY" ]; then
    ok "ambiente já existe (.venv)"
else
    python=$(achar_python || true)
    if [ -z "$python" ]; then
        aviso "Nenhum Python 3.10+ encontrado ($(python3 --version 2>&1)); instalando o 3.12..."
        case "$PM" in
            apt) instalar_pacotes "python3.12 python3.12-venv" "" "" ;;
            dnf) $SUDO dnf install -y -q python3.12 python3.12-pip ;;
            pacman) instalar_pacotes "" "" "python" ;;
        esac
        python=$(achar_python || true)
        [ -n "$python" ] || falha "Não consegui instalar um Python 3.10+. Instale o python3.12 pelo gerenciador de pacotes e rode de novo."
    fi
    ok "usando $python ($("$python" --version 2>&1))"
    "$python" -m venv .venv || falha "Não consegui criar o ambiente (.venv). Instale o pacote venv do $python."
fi

# --- 4. Bibliotecas ----------------------------------------------------------------
passo "Bibliotecas do app (alguns minutos na 1ª vez)"
"$PY" -m pip install --upgrade pip --quiet --disable-pip-version-check
"$PY" -m pip install -r requirements.txt --quiet --disable-pip-version-check || falha "Falha ao instalar requirements.txt."
ok "bibliotecas instaladas"
# Janela própria (Qt): opcional — sem ela o app abre no navegador
if "$PY" -m pip install "pywebview[qt]>=5.0" --quiet --disable-pip-version-check; then
    ok "janela própria (Qt)"
else
    aviso "Não deu pra instalar a janela Qt nesta distribuição; o app vai abrir no navegador."
fi

nvidia=""
if command -v nvidia-smi >/dev/null 2>&1; then
    nvidia=$(nvidia-smi --query-gpu=name --format=csv,noheader 2>/dev/null | head -1 || true)
fi
if [ -n "$nvidia" ]; then
    ok "placa NVIDIA: $nvidia"
    "$PY" -m pip install -r requirements-gpu.txt --quiet --disable-pip-version-check || falha "Falha ao instalar as bibliotecas CUDA."
    ok "bibliotecas CUDA instaladas"
else
    aviso "Sem placa NVIDIA (ou sem driver): a transcrição vai rodar no processador (mais lenta, modelo small)."
    salvar_config '{"whisper_dispositivo": "cpu", "whisper_modelo": "small", "local_dispositivo": "cpu", "intervalo": 120}'
fi

# --- 5. Modelo de transcrição -------------------------------------------------------
whisper=$([ -n "$nvidia" ] && echo "large-v3-turbo" || echo "small")
passo "Baixando o modelo de transcrição ($whisper)"
HF_HUB_DISABLE_IMPLICIT_TOKEN=1 "$PY" -c "from faster_whisper.utils import download_model; download_model('$whisper')" \
    || falha "Falha ao baixar o modelo do Whisper."
ok "modelo pronto"

# --- 6. Modo local (Ollama) ---------------------------------------------------------
passo "Modo local (Ollama) — explicações sem internet e sem custo"
if [ -z "$OLLAMA_OPCAO" ]; then
    read -r -p "    Instalar o modo local? Baixa ~5 GB (Ollama + modelo $MODELO_LOCAL). [S/n] " r
    case "$r" in ""|[sSyY]*) OLLAMA_OPCAO=sim ;; *) OLLAMA_OPCAO=nao ;; esac
fi
if [ "$OLLAMA_OPCAO" = "sim" ]; then
    if ! command -v ollama >/dev/null 2>&1; then
        echo "    instalando o Ollama pelo instalador oficial (ollama.com)..."
        curl -fsSL https://ollama.com/install.sh | sh || falha "Falha ao instalar o Ollama."
    fi
    ok "Ollama instalado"
    # Garante o servidor no ar (o instalador oficial cria um serviço do systemd)
    for i in $(seq 1 30); do
        if curl -fs http://127.0.0.1:11434/api/tags >/dev/null; then break; fi
        [ "$i" -eq 1 ] && (nohup ollama serve >/dev/null 2>&1 &)
        sleep 1
    done
    curl -fs http://127.0.0.1:11434/api/tags >/dev/null || falha "O Ollama não respondeu. Rode 'ollama serve' e o instalador de novo."
    ollama pull "$MODELO_LOCAL" || falha "Falha ao baixar o modelo $MODELO_LOCAL."
    salvar_config "{\"modelo_local\": \"$MODELO_LOCAL\"}"
    ok "modelo $MODELO_LOCAL pronto"
else
    aviso "Pulado. O app vai usar o Claude: cole sua chave da API em Ajustes."
    salvar_config '{"cerebro": "claude"}'
fi

# --- 7. Atalho ------------------------------------------------------------------------
chmod +x "$RAIZ/copiloto.sh"
if [ -z "$SEM_ATALHO" ]; then
    passo "Atalho no menu de aplicativos"
    apps="${XDG_DATA_HOME:-$HOME/.local/share}/applications"
    mkdir -p "$apps"
    entrada="$apps/copiloto-de-reunioes.desktop"
    cat > "$entrada" <<EOF
[Desktop Entry]
Type=Application
Name=Copiloto de Reuniões
Comment=Transcreve, explica e escreve a ata das suas reuniões
Exec="$RAIZ/copiloto.sh"
Path=$RAIZ
Icon=audio-input-microphone
Terminal=false
Categories=Office;AudioVideo;
EOF
    chmod +x "$entrada"
    ok "$entrada"
    area=$(xdg-user-dir DESKTOP 2>/dev/null || echo "$HOME/Desktop")
    if [ -d "$area" ]; then
        cp "$entrada" "$area/"
        chmod +x "$area/copiloto-de-reunioes.desktop"
        # GNOME: marca como confiável pra abrir com duplo clique
        gio set "$area/copiloto-de-reunioes.desktop" metadata::trusted true 2>/dev/null || true
        ok "também na Área de Trabalho ($area)"
    fi
fi

printf '\n\033[32mPronto! Abra "Copiloto de Reuniões" pelo menu de aplicativos (ou ./copiloto.sh).\033[0m\n'
