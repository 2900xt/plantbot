# plantbot

Raspberry Pi plant-health rover for MakeCU 2026: camera + CNN health score, MQ-2 and BME680 VOC sniffing, phone dashboard.
The design (interfaces, state machine, pin map) is in the brain repo: `plantbot-brain/software/README.md`.

## Run it on a laptop (no hardware)

```bash
uv venv .venv && uv pip install --python .venv/bin/python -r requirements.txt   # or python -m venv + pip
.venv/bin/python -m plantbot --mock
```

Open http://localhost:8000/. In mock mode every device is fake:

| Device | Mock |
|---|---|
| Camera | `FolderCamera`: cycles photos in `test_images/` (synthetic plants if the folder is empty). Each Scan advances to the next photo. Use `--camera opencv` for your webcam. |
| Motors | `MockMotors`: speeds shown in the video HUD. The watchdog runs exactly like on the Pi. |
| MQ-2 / BME680 | `MockEnv`: drift and noise, plus scripted VOC events (`mock.env_script` in config.yaml) and a "Mock VOC spike" button |
| Ultrasonic | `MockRanger`: a virtual row of plants every 60 cm, so AUTO mode works |
| Classifier | `dummy` heuristic (share of yellow/brown pixels). A stand-in until the real model arrives. |

Try it: press **Scan** (or Space) for a normal plant; press **Mock VOC spike**, wait 3 s, then Scan, and the score drops on the smell term. Drive with WASD or the pad; let go, or close the tab, and the watchdog stops the motors within 0.5 s.

## Layout

```
config.yaml              all pins, addresses, thresholds, weights. Change hardware here, not in code.
plantbot/__main__.py     python -m plantbot [--mock] [--config f] [--camera folder|opencv|picamera] [--port N]
plantbot/hal/            one file per device: real driver (hardware libs imported lazily) + mock
plantbot/sensing/        sampler thread: 10 Hz ring buffer, warm-up, baseline, sniff()
plantbot/vision/         isolate.py (nearest-plant crop), classify.py (dummy/tflite/onnx/remote)
plantbot/fusion.py       health = 100 * (0.7 * p_healthy + 0.3 * (1 - voc_stress))
plantbot/mission.py      state machine: WARMUP, IDLE, scan flow, AUTO, ESTOP
plantbot/store.py        scans/scans.db + scans/<id>/{raw.jpg, mask.png, crop.jpg, scan.json}; scans/sensors.csv at 1 Hz
plantbot/web/            FastAPI server + single-file dashboard (vanilla JS, no build)
tools/hw_check.py        spin each motor, read each sensor, take a photo. Run first, every time.
tools/capture_dataset.py labeled photos from the rover camera (hotkeys 1-9), for training data
tools/calibrate.py       print clean-air sensor values to sanity-check warm-up and RL
```

Any hardware that fails to start is logged, shown red on the dashboard, and swapped for its mock, so a dead BME680 doesn't stop driving.

## Pi setup

Raspberry Pi OS Bookworm 64-bit. On a Pi 5, `RPi.GPIO` doesn't work; we use gpiozero everywhere, so that's fine.

```bash
sudo raspi-config nonint do_i2c 0          # enable I2C (camera is on by default on Bookworm)
sudo apt update && sudo apt install -y git i2c-tools python3-venv python3-gpiozero python3-lgpio python3-picamera2
git clone <this repo> ~/plantbot && cd ~/plantbot
python3 -m venv --system-site-packages .venv    # system site-packages so it sees gpiozero/picamera2 from apt
.venv/bin/pip install -r requirements-pi.txt
i2cdetect -y 1                                  # expect 0x48 (ADS1115) and 0x76/0x77 (BME680)
```

1. Edit `config.yaml`: `motors.driver` (servo / gpiozero / serial), pins, `camera.driver` (opencv for the USB webcam, picamera for CSI), `env.bme680_address`, `env.mq2.rl_kohm` (measure the module's load resistor).
2. `.venv/bin/python tools/hw_check.py`: every check should PASS. Flip `invert_left/right` if a wheel spins backwards.
3. `.venv/bin/python -m plantbot` and open `http://plantbot.local:8000/` (or the IP printed in the log) from a phone on the same hotspot.
4. Install the service so it starts on boot and restarts on crash:
   ```bash
   sudo cp systemd/plantbot.service /etc/systemd/system/ && sudo systemctl enable --now plantbot
   journalctl -u plantbot -f
   ```
   Deploy: `git pull && sudo systemctl restart plantbot`. Stop the service before running `hw_check.py` or `capture_dataset.py`, because the service holds the camera and GPIO.

## Plugging in the model (ML is owned by a teammate)

This repo doesn't train anything. To plug a model in, drop it in `models/` and set `classifier.backend`:

- `models/plant.tflite` or `models/plant.onnx`, plus `models/labels.txt` (one label per line, must include `healthy`)
- set `classifier.input_size` and `classifier.normalize` (`0-1`, `-1-1`, or `imagenet`) to match training. Get this wrong and the model silently outputs garbage.
- Pi too slow, or the runtime won't install? Run `python -m plantbot.vision.classify --serve --backend onnx` on a laptop and set `backend: remote` + `remote_url`.
- Quick test: `python -m plantbot.vision.classify path/to/leaf.jpg`.

If the backend fails to load, the app logs it, shows `classifier: fallback-dummy`, and keeps running.

## WebSocket protocol (`/ws`)

Client → robot: `{"drive":[l,r]}` (-1..1; resend every 100 ms while held), `{"cmd":"scan"|"auto_on"|"auto_off"|"estop"|"reset"|"recalibrate"|"mock_spike"}`.
Robot → client: `{"telemetry":{state, mode, warm, env, ratios, baseline, range_cm, motors, devices, ...}}` at 5 Hz, `{"scan_progress":{...}}`, `{"scan":{...}}`.
REST: `GET /api/status`, `GET /api/scans`, `GET /api/scans/{id}`, images under `/scans/<id>/`.

## Not done yet / unverified
- Everything was tested in `--mock` only. The real drivers (servo, H-bridge, serial, BME680, ADS1115, HC-SR04, picamera2) are written to their library docs but haven't touched hardware.
- AUTO mode is a stub: it works against the mock row, but thresholds aren't tuned.
- Isolation thresholds in `config.yaml` → `vision` were tuned on synthetic images only. Re-tune on real photos (a brown wooden table is the likely trap).
- Status LED and the warm-up OLED aren't implemented.
