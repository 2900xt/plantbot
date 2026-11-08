#!/usr/bin/env bash
# One-time (idempotent) Pi setup. Runs ON the Pi; deploy.sh calls it with --setup.
# Installs apt packages, enables I2C, makes the venv, installs the systemd unit.
set -euxo pipefail
APP_DIR="$(cd "$(dirname "$0")/.." && pwd)"
APP_USER="$(id -un)"

echo "== apt packages"
sudo apt-get update
sudo DEBIAN_FRONTEND=noninteractive apt-get install -y \
  git i2c-tools python3-venv python3-dev python3-gpiozero python3-lgpio python3-picamera2 v4l-utils

echo "== I2C"
sudo raspi-config nonint do_i2c 0
sudo usermod -aG i2c,gpio,video,dialout "$APP_USER"

echo "== venv"
cd "$APP_DIR"
[ -d .venv ] || python3 -m venv --system-site-packages .venv
.venv/bin/pip install --upgrade pip
.venv/bin/pip install -r requirements-pi.txt
sha256sum requirements.txt requirements-pi.txt > .venv/.req-hash

echo "== systemd"
sed -e "s|@USER@|$APP_USER|g" -e "s|@DIR@|$APP_DIR|g" systemd/plantbot.service \
  | sudo tee /etc/systemd/system/plantbot.service >/dev/null
sudo systemctl daemon-reload
sudo systemctl enable plantbot
echo "setup done (reboot once if I2C was just enabled: /dev/i2c-1 must exist)"
