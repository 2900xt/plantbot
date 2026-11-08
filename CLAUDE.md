# plantbot

Rover software for our MakeCU 2026 Pi plant-health rover. The design lives in the brain repo, `../plantbot-brain/software/README.md`; read it before changing architecture, interfaces, or the state machine. Team status and decisions also live in the brain repo (`STATUS.md`, `DECISIONS.md`).

- Run on a laptop with `python -m plantbot --mock`. Keep mock mode working: teammates without the Pi depend on it.
- Hardware choices (pins, drivers, I2C addresses, thresholds) go in `config.yaml`, not in code.
- **Always commit and push.** After any change, commit it in small, logical commits (one idea each, clear message) and push to `origin` right away. Teammates pull from GitHub; unpushed work doesn't exist for them. Deploy to the Pi with `./deploy.sh` (rsync over SSH, see README).
- Keep credentials, Wi-Fi passwords, and API keys out of this repo.
