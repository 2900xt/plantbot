#!/usr/bin/env bash
set -euo pipefail
task_root="$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)"
cd "$task_root"
if [ "$(uname -s)" != Linux ] || [ "$(uname -m)" != x86_64 ]; then
  echo 'This launcher supports x64 Linux. Use Start-LeafLink.cmd on x64 Windows.' >&2; exit 1
fi
if ! command -v nvidia-smi >/dev/null 2>&1; then
  echo 'Install a working NVIDIA driver first: https://www.nvidia.com/Download/index.aspx' >&2; exit 1
fi
if command -v node >/dev/null 2>&1 && node -e 'process.exit(Number(process.versions.node.split(".")[0])>=24?0:1)'; then
  task_node="$(command -v node)"
else
  task_tools="$task_root/.leaflink/tools"
  mkdir -p "$task_tools"
  task_node="$(find "$task_tools" -maxdepth 3 -path '*/node-v24*-linux-x64/bin/node' -type f -print -quit)"
  if [ -z "$task_node" ]; then
    echo 'Installing project-local Node.js 24...'
    task_sums="$task_tools/node-SHASUMS256.txt"
    curl -fSL --retry 2 https://nodejs.org/dist/latest-v24.x/SHASUMS256.txt -o "$task_sums"
    task_filename="$(awk '$2 ~ /^node-v24\.[0-9]+\.[0-9]+-linux-x64\.tar\.xz$/ {print $2;exit}' "$task_sums")"
    if [ -z "$task_filename" ]; then echo 'Official Node 24 checksum not found.' >&2;exit 1;fi
    task_version="${task_filename#node-}";task_version="${task_version%-linux-x64.tar.xz}"
    curl -fSL --retry 2 "https://nodejs.org/dist/$task_version/$task_filename" -o "$task_tools/$task_filename"
    (cd "$task_tools" && awk -v file="$task_filename" '$2 == file' node-SHASUMS256.txt | sha256sum -c -)
    tar -xf "$task_tools/$task_filename" -C "$task_tools"
    task_node="$task_tools/${task_filename%.tar.xz}/bin/node"
  fi
fi
export PATH="$(dirname "$task_node"):$PATH"
exec "$task_node" "$task_root/setup.js" --start "$@"
