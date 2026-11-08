#!/usr/bin/env bash
# Render the Vultr user-data and print (or, with --yes, run) the vultr-cli commands.
#   ./launch.sh                 dry run: writes user-data.yaml, prints the commands, creates NOTHING
#   ./launch.sh --yes           actually creates the instance (costs money until you delete it)
# Needs: vultr-cli (https://github.com/vultr/vultr-cli), VULTR_API_KEY in the environment.
# Overrides: REGION=ewr PLAN=vc2-1c-2gb OS_ID=2284 LABEL=leaflink SSH_KEY_ID=<id>
set -euo pipefail
cd "$(dirname "$0")"

REGION="${REGION:-ewr}"            # ewr = New Jersey, closest to Columbia
PLAN="${PLAN:-vc2-1c-2gb}"         # 1 vCPU / 2 GB: enough for npm build + the Node server
OS_ID="${OS_ID:-2284}"             # Ubuntu 24.04 LTS x64. Verify: vultr-cli os list | grep -i 'ubuntu 24'
LABEL="${LABEL:-leaflink}"

# Paste Dockerfile.cpu into the cloud-init template, indented to match the YAML block.
awk 'NR==FNR { df = df (FNR > 1 ? "\n      " : "") $0; next }
     /^ *@DOCKERFILE@$/ && (i = index($0, "@DOCKERFILE@")) { $0 = substr($0, 1, i - 1) df substr($0, i + 12) } { print }' \
    Dockerfile.cpu cloud-init.yaml > user-data.yaml
echo "wrote $(pwd)/user-data.yaml ($(wc -c < user-data.yaml) bytes)"

ssh_flag=()
[ -n "${SSH_KEY_ID:-}" ] && ssh_flag=(--ssh-keys "$SSH_KEY_ID")
cmd=(vultr-cli instance create --region "$REGION" --plan "$PLAN" --os "$OS_ID" --label "$LABEL"
     --host "$LABEL" --userdata "$(cat user-data.yaml)" "${ssh_flag[@]}")

if [ "${1:-}" != "--yes" ]; then
  cat <<EOF

DRY RUN (nothing created). To launch:
  export VULTR_API_KEY=...            # https://my.vultr.com/settings/#settingsapi (allow your IP)
  vultr-cli ssh-key create --name team --key "\$(cat ~/.ssh/id_ed25519.pub)"   # once; note the id
  SSH_KEY_ID=<id> ./launch.sh --yes
Equivalent raw command:
  vultr-cli instance create --region $REGION --plan $PLAN --os $OS_ID --label $LABEL --host $LABEL \\
    --userdata "\$(cat user-data.yaml)" ${SSH_KEY_ID:+--ssh-keys $SSH_KEY_ID}
EOF
  exit 0
fi

[ -n "${VULTR_API_KEY:-}" ] || { echo "set VULTR_API_KEY"; exit 1; }
"${cmd[@]}"
cat <<'EOF'

Instance requested. ~3-6 min for boot + docker build. Then:
  vultr-cli instance list                         # main IP
  ssh root@<ip> cat /opt/leaflink/URL             # dashboard URL (https://<ip-dashed>.sslip.io)
  ssh root@<ip> grep API_TOKEN /opt/leaflink/.env # token for the dashboard + the rover's LEAFLINK_TOKEN
  ssh root@<ip> tail -f /var/log/leaflink-bootstrap.log
When done:  vultr-cli instance delete <instance-id>   (billing stops only when it is DELETED)
EOF
