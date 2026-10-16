#!/usr/bin/env bash
# Abre o Copiloto de Reuniões no Linux. Aberto pelo menu (sem terminal), a
# saída vai pra dados/copiloto.log.
RAIZ="$(cd "$(dirname "$(readlink -f "${BASH_SOURCE[0]}")")" && pwd)"
cd "$RAIZ" || exit 1
if [ -t 1 ]; then
    exec "$RAIZ/.venv/bin/python" app.py "$@"
fi
mkdir -p dados
exec "$RAIZ/.venv/bin/python" app.py "$@" >>dados/copiloto.log 2>&1
