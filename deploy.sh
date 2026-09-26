#!/usr/bin/env bash
# Deploy RaspiDR to the Pi. Host and directory come from .env (PI_HOST, PI_DIR — see .env.example).
#   ./deploy.sh             sync code, models and sounds
#   ./deploy.sh --install   + install/update Python dependencies in .venv on the Pi
#   ./deploy.sh --restart   + restart the assistant in the background (log: assistant.log on the Pi)
#   ./deploy.sh --logs      only follow the assistant log (no sync)
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
  rsync -az --itemize-changes \
    --exclude .git --exclude .github --exclude .venv --exclude .venv-train --exclude __pycache__ --exclude .DS_Store \
    --exclude hey-peedor --exclude recordings --exclude training --exclude node_modules \
    --exclude docs/.vitepress/cache --exclude docs/.vitepress/dist \
    ./ "$HOST:$DIR/"
fi

if (( install )); then
  ssh "$HOST" "cd $DIR && { [ -d .venv ] || python3 -m venv --system-site-packages .venv; } \
    && .venv/bin/pip install -q -r requirements.txt \
    && .venv/bin/python -c 'import openwakeword.utils as u; u.download_models()' >/dev/null 2>&1"
  echo "dependencies on $HOST updated"
fi

if (( restart )); then
  # the pattern must not match this ssh command's own command line — hence [a]ssistant
  ssh "$HOST" 'pkill -f "python -u src/[a]ssistant.py"; for i in 1 2 3 4 5; do pgrep -f "python -u src/[a]ssistant.py" >/dev/null || break; sleep 1; done'
  # -n and local redirects: the backgrounded ssh must not hold this script's stdout (hangs pipes like `| tail`)
  ssh -f -n "$HOST" "cd $DIR && setsid nohup .venv/bin/python -u src/assistant.py > assistant.log 2>&1 < /dev/null &" >/dev/null 2>&1
  echo "assistant restarted on $HOST (./deploy.sh --logs to follow)"
fi

if (( logs )); then
  ssh "$HOST" "tail -n 30 -f $DIR/assistant.log"
fi
