#!/usr/bin/env bash
# Deploy this checkout to the Pi and restart the service.
#   ./deploy.sh            sync code, reinstall deps if requirements changed, restart, health-check
#   ./deploy.sh --setup    first time on a fresh Pi (apt, I2C, venv, systemd), then deploy
#   ./deploy.sh --logs     tail the service log
# Override the target: PI=pi@plantbot.local ./deploy.sh
set -euo pipefail
PI="${PI:-taha@vittles.local}"
DIR="${PI_DIR:-plantbot}"          # relative to the Pi user's home
cd "$(dirname "$0")"

if [ "${1:-}" = "--logs" ]; then exec ssh -t "$PI" "journalctl -u plantbot -f -n 50"; fi

if [ -n "$(git status --porcelain)" ]; then
  echo "note: deploying uncommitted changes (commit + push so teammates have them)"
fi

echo "== sync -> $PI:~/$DIR"
# config.yaml is synced too: the repo copy is the source of truth for the rover.
rsync -az --delete --itemize-changes --stats --human-readable \
  --exclude .git/ --exclude .venv/ --exclude __pycache__/ --exclude '*.pyc' \
  --exclude scans/ --exclude dataset/ --exclude leaflink/ --exclude hw_check.jpg \
  --exclude 'calibration/crosscal.json' --exclude .env \
  ./ "$PI:$DIR/"
# models/*.tflite|onnx are gitignored but still synced if present locally.

if [ "${1:-}" = "--setup" ]; then ssh -t "$PI" "bash $DIR/deploy/setup_pi.sh"; fi

ssh "$PI" bash -s -- "$DIR" <<'REMOTE'
set -euo pipefail
cd "$HOME/$1"
if [ ! -d .venv ]; then echo "no venv on the Pi: run ./deploy.sh --setup"; exit 1; fi
if ! sha256sum -c --quiet .venv/.req-hash 2>/dev/null; then
  echo "== requirements changed, pip install"
  .venv/bin/pip install -r requirements-pi.txt
  sha256sum requirements.txt requirements-pi.txt > .venv/.req-hash
fi
unit=$(sed -e "s|@USER@|$(id -un)|g" -e "s|@DIR@|$PWD|g" systemd/plantbot.service)
if [ "$unit" != "$(cat /etc/systemd/system/plantbot.service 2>/dev/null)" ]; then
  echo "== systemd unit changed, reinstalling"
  echo "$unit" | sudo tee /etc/systemd/system/plantbot.service >/dev/null
  sudo systemctl daemon-reload
fi
echo "== restart"
sudo systemctl restart plantbot
echo "== waiting for :8000"
for i in $(seq 1 20); do
  if curl -fs -o /dev/null http://localhost:8000/api/status; then
    journalctl -u plantbot -n 25 --no-pager -o cat
    echo "OK: plantbot is up -> http://$(hostname).local:8000/  ($(hostname -I | awk '{print $1}'))"
    exit 0
  fi
  sleep 1
done
echo "FAILED: no response on :8000 after 20 s. Last log lines:"
journalctl -u plantbot -n 40 --no-pager
exit 1
REMOTE
