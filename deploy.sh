#!/usr/bin/env bash
# Deploy RaspiDR to the Pi. Host and directory come from .env (PI_HOST, PI_DIR — see .env.example).
#   ./deploy.sh             sync code, models and sounds
#   ./deploy.sh --install   + raspidr.sh install on the Pi: Python dependencies, systemd units (enable + start)
#   ./deploy.sh --restart   + raspidr.sh restart: restart the knob and the assistant units
#   ./deploy.sh --logs      only follow the journal of both units (no sync)
# On the Pi itself: ./raspidr.sh install | uninstall | start | stop | restart | status | logs [-f]
set -euo pipefail
cd "$(dirname "$0")"
if [[ -f .env ]]; then
  set -a
  . ./.env
  set +a
fi
HOST="${PI_HOST:?set PI_HOST in .env (see .env.example)}"
DIR="${PI_DIR:-raspidr}"

install=0 restart=0 logs=0
for arg in "$@"; do
  case "$arg" in
    --install) install=1 ;;
    --restart) restart=1 ;;
    --logs) logs=1 ;;
    *) echo "unknown option: $arg" >&2; exit 2 ;;
  esac
done

if (( !logs || install || restart )); then
  # .env holds the API keys and the /say token; rsync -a carries its mode over to the Pi (and fixes an existing copy)
  [[ -f .env ]] && chmod 600 .env
  rsync -az --itemize-changes \
    --exclude .git --exclude .github --exclude .venv --exclude .venv-train --exclude __pycache__ --exclude .DS_Store \
    --exclude hey-peedor --exclude recordings --exclude training --exclude node_modules \
    --exclude docs/.vitepress/cache --exclude docs/.vitepress/dist \
    ./ "$HOST:$DIR/"
fi

if (( install )); then
  ssh "$HOST" "cd $DIR && ./raspidr.sh install"
elif (( restart )); then
  ssh "$HOST" "cd $DIR && ./raspidr.sh restart && ./raspidr.sh status"
fi

if (( logs )); then
  ssh "$HOST" "cd $DIR && ./raspidr.sh logs -f"
fi
