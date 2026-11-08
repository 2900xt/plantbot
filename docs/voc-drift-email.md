**Subject:** VOC drift detector is on main: deploy + how to use it

Hey,

I pushed a VOC drift detector for the MQ-2 + BME680, plus a "VOC drift (σ)" card on the LeafLink Field Overview.

**What it does**
After warm-up, the rover spends 60 s learning "healthy air" (the mean and std dev of each sensor's ln(resistance), corrected for humidity). Then every second it scores how many σ the air has moved from that baseline (z-score), and keeps a running total (CUSUM) so slow drift gets caught too.
- **Healthy:** both sensors under 2σ.
- **Watch:** either sensor over 2σ.
- **Diseased:** both sensors over 3σ (or both CUSUMs over their limit) for 5 s. Each episode also shows up once under VOC anomalies.

Code: `plantbot/sensing/voc_drift.py`. Settings: `config.yaml` → `sensing.drift`.

**Deploy**
1. Rover: `git pull`, then `./deploy.sh` from the laptop. Once the MQ-2/BME680 are wired, set `env.driver: real` in `config.yaml` first (it's `none` right now).
2. LeafLink laptop: `git pull`, then in `leaflink/` run `npm run build` and restart LeafLink (`npm start`). The build updates the dashboard; the restart adds the new `/api/telemetry/drift` endpoint.

**Using it**
- Start the rover **next to a healthy plant or in clean air**. Whatever it sees during the 60 s calibration counts as normal. The card shows "Learning healthy air… x%", then the verdict.
- Recalibrate after moving to a new room or field: send `{"cmd":"drift_recalibrate"}` over the rover websocket, or delete `calibration/voc_drift.json` on the Pi and restart. Deploys keep this file.
- Clear an alarm you've already checked: `{"cmd":"drift_reset"}`.
- Demo without hardware: `python -m plantbot --mock` with `LEAFLINK_URL`/`LEAFLINK_TOKEN` set. The mock VOC spike drives the card all the way to "DISEASE?" and back to healthy about 20 s later.

Heads up: the thresholds are textbook starting values and haven't been tuned on real plants yet. It only reports "the air here changed"; the camera decides what's actually wrong.

Taha
