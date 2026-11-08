# LeafLink dashboard on Vultr

Runs the team's LeafLink dashboard (field map, VOC telemetry, rover photo inbox, event rules) on a
Vultr cloud VM so the rover and everyone's phone can reach it from anywhere over HTTPS, instead of
only on the GPU laptop's LAN.

Nothing here has been created on Vultr. `launch.sh` is a dry run unless you pass `--yes`.

## What runs

| | |
|---|---|
| VM | Vultr Cloud Compute `vc2-1c-2gb` (1 vCPU, 2 GB), Ubuntu 24.04, region `ewr` (New Jersey) |
| Containers | `leaflink` (built from `Dockerfile.cpu`, Node 24, no torch/CUDA, ~270 MB) + `caddy` (HTTPS) |
| URL | `https://<ip-with-dashes>.sslip.io`: free wildcard DNS, real Let's Encrypt certificate, no domain needed |
| Secrets | `API_TOKEN` generated on the box at first boot, stored in `/opt/leaflink/.env` (root only). Never in user-data, which Vultr exposes through instance metadata. |
| Model | `MODEL_MODE=disabled`, `LEAF_CROP_MODE=disabled`: photos are stored and mapped, the leaf verdict comes from the rover's on-device classifier. Set `MODEL_MODE=demo` in `/opt/leaflink/.env` for simulated verdicts (the UI marks them simulated). |

`Dockerfile.cpu` was built and smoke-tested locally against LeafLink `f852f8c`: `/api/health` 200,
`/` 200, `/api/config` 401 without a token.

### Why not a Vultr Cloud GPU

The full LeafLink pipeline (SegFormer-B2 + epoch-8 CUDA classifier) needs a CUDA GPU **and** the two
weight files from `LeafLink/models/README.md`, which are not in git, so you'd have to upload them
too. A GPU instance costs dollars per hour and its build pulls about 3 GB of CUDA torch. For a
hackathon demo, the CPU box at about $10/month (about $0.015/hour, billed hourly) does everything
the judges see. If you want it anyway: `vultr-cli plans list --type vcg`, then build
`LeafLink/Dockerfile.gpu` with `compose.gpu.yaml` on that box.

## Launch (about 5 minutes)

```bash
# 0. account: https://www.vultr.com (ask the MLH table for a Vultr credit code first)
#    API key: Account -> API -> Enable API, and allow your current IP
# 1. CLI: https://github.com/vultr/vultr-cli/releases  (or: brew install vultr/vultr-cli/vultr-cli)
export VULTR_API_KEY=...                       # in your shell only, never in a file in the repo
vultr-cli os list | grep -i 'ubuntu 24.04'     # confirm OS id 2284 (else OS_ID=<id>)
vultr-cli plans list | grep vc2-1c-2gb         # confirm the plan and its price
vultr-cli ssh-key create --name team --key "$(cat ~/.ssh/id_ed25519.pub)"   # note the id

# 2. dry run: renders user-data.yaml and prints the exact command
cd plantbot/deploy/vultr && ./launch.sh

# 3. create it (starts billing)
SSH_KEY_ID=<id> ./launch.sh --yes
```

Equivalent raw command:

```bash
./launch.sh   # writes user-data.yaml
vultr-cli instance create --region ewr --plan vc2-1c-2gb --os 2284 \
  --label leaflink --host leaflink --ssh-keys <ssh-key-id> --userdata "$(cat user-data.yaml)"
```

## After boot

```bash
vultr-cli instance list                          # MAIN IP + instance ID
ssh root@<ip> tail -f /var/log/leaflink-bootstrap.log   # wait for "docker compose up" to finish
ssh root@<ip> cat /opt/leaflink/URL              # https://<ip-dashed>.sslip.io
ssh root@<ip> grep API_TOKEN /opt/leaflink/.env  # paste into the dashboard's Connection box
```

Point the rover at it (on the Pi, `~/plantbot/.env`, then `sudo systemctl restart plantbot`):

```
LEAFLINK_URL=https://<ip-dashed>.sslip.io
LEAFLINK_TOKEN=<API_TOKEN from the box>
```

Change settings: edit `/opt/leaflink/.env` on the box, then
`cd /opt/leaflink && docker compose up -d`. Update the code:
`cd /opt/leaflink/src && git pull && cd .. && docker compose up -d --build`.

## Tear down (billing stops only when the instance is deleted)

```bash
vultr-cli instance delete <instance-id>
```

## Files

- `Dockerfile.cpu`: CPU image for LeafLink (build context = a LeafLink checkout)
- `cloud-init.yaml`: user-data template (installs Docker from Ubuntu packages, clones LeafLink, writes compose + Caddyfile, opens 22/80/443 with ufw, `docker compose up`)
- `launch.sh`: renders `user-data.yaml` (generated, safe to delete, no secrets) and prints or runs `vultr-cli`
