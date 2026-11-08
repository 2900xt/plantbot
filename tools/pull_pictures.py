#!/usr/bin/env python3
"""Copy photos taken by tools/wheel_capture.py from the Pi to this laptop, verified.

    python pull_pictures.py --watch          # keep copying new photos as they are taken
    python pull_pictures.py                  # copy whatever is new once, then exit
    python pull_pictures.py --dest D:/photos --pi pi@plantbot.local

Runs on the laptop (Windows, macOS or Linux): only needs Python 3 and the ssh/sftp
commands, with key login to the Pi set up (no password prompt).

Reliability:
  * Each file is downloaded as NAME.jpg.part and renamed to NAME.jpg only after its
    size matches the Pi's copy and it ends with a JPEG end marker. Anything that
    processes the folder should only read *.jpg files: those are always complete.
  * The Pi side writes photos under a hidden temp name and renames them when done,
    so a photo that is still being written is never listed.
  * Failed or partial copies are retried on the next pass; nothing is deleted on the Pi.

Layout: <dest>/<session start time>/0001.jpg, 0002.jpg, ...  (same as on the Pi)
Expect new photos on the laptop within ~3-6 s of being taken.
"""
from __future__ import annotations

import argparse
import os
import socket
import subprocess
import sys
import time
from pathlib import Path

SSH_OPTS = ["-o", "BatchMode=yes", "-o", "ConnectTimeout=8"]
_resolved: list[str] = []  # extra ssh options pinning the Pi's IP (see ssh_opts)


def ssh_opts(pi: str) -> list[str]:
    """Resolve the Pi's name once and connect by IP afterwards.

    Looking up NAME.local (mDNS) can take 10+ s per connection on Windows, which made
    every pass take ~30 s. HostKeyAlias keeps the known_hosts check on the real name.
    """
    if not _resolved:
        cfg = subprocess.run(["ssh", "-G", pi], capture_output=True, text=True).stdout
        host = next((l.split(None, 1)[1] for l in cfg.splitlines() if l.startswith("hostname ")), pi)
        try:
            ip = socket.getaddrinfo(host, 22, type=socket.SOCK_STREAM)[0][4][0]
            _resolved.extend(["-o", f"HostName={ip}", "-o", f"HostKeyAlias={host}"])
            print(f"Pi {host} is at {ip}")
        except socket.gaierror as e:
            raise ConnectionError(f"can't find {host}: {e}") from None
    return SSH_OPTS + _resolved


def remote_listing(pi: str, remote_dir: str) -> dict[str, int]:
    """{'<session>/<file>.jpg': size} for every finished photo on the Pi."""
    cmd = f'cd ~/{remote_dir} 2>/dev/null && find . -name "*.jpg" -printf "%P\\t%s\\n" || true'
    out = subprocess.run(["ssh", *ssh_opts(pi), pi, cmd], capture_output=True, text=True, timeout=30)
    if out.returncode != 0:
        raise ConnectionError(out.stderr.strip() or f"ssh exited {out.returncode}")
    files = {}
    for line in out.stdout.splitlines():
        rel, _, size = line.partition("\t")
        if rel and size.isdigit():
            files[rel] = int(size)
    return files


def is_complete_jpeg(path: Path, size: int) -> bool:
    if not path.exists() or path.stat().st_size != size or size < 4:
        return False
    with open(path, "rb") as f:
        f.seek(-2, os.SEEK_END)
        return f.read(2) == b"\xff\xd9"


def pull_once(pi: str, remote_dir: str, dest: Path) -> tuple[int, int]:
    """Copy missing photos. Returns (copied, failed)."""
    remote = remote_listing(pi, remote_dir)
    todo = sorted(rel for rel, size in remote.items() if not is_complete_jpeg(dest / rel, size))
    if not todo:
        return 0, 0
    batch = []
    for rel in todo:
        local = dest / rel
        local.parent.mkdir(parents=True, exist_ok=True)
        part = local.with_name(local.name + ".part")
        batch.append(f'-get "{remote_dir}/{rel}" "{part.as_posix()}"')  # "-" = keep going on error
    # one sftp connection for the whole batch (much faster than one scp per photo)
    subprocess.run(["sftp", *ssh_opts(pi), "-q", "-b", "-", pi], input="\n".join(batch) + "\n",
                   capture_output=True, text=True, timeout=60 + 5 * len(todo))
    copied = failed = 0
    for rel in todo:
        local = dest / rel
        part = local.with_name(local.name + ".part")
        if is_complete_jpeg(part, remote[rel]):
            os.replace(part, local)
            copied += 1
            print(f"  got {rel}")
        else:
            failed += 1
            part.unlink(missing_ok=True)
            print(f"  RETRY {rel} (incomplete or failed copy)")
    return copied, failed


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--pi", default="vittles", help="ssh host (default: the 'vittles' entry in ~/.ssh/config)")
    ap.add_argument("--remote-dir", default="plantbot/Pictures_Test", help="relative to the Pi user's home")
    ap.add_argument("--dest", default=str(Path(__file__).resolve().parent / "Pictures_Test"),
                    help="local folder (default: Pictures_Test next to this script)")
    ap.add_argument("--watch", action="store_true", help="keep running and copy new photos as they appear")
    ap.add_argument("--interval", type=float, default=2.0, help="seconds between checks in --watch mode")
    a = ap.parse_args()
    dest = Path(a.dest).resolve()
    dest.mkdir(parents=True, exist_ok=True)
    print(f"Pi {a.pi}:~/{a.remote_dir}  ->  {dest}")

    total, offline = 0, False
    while True:
        try:
            copied, failed = pull_once(a.pi, a.remote_dir, dest)
            total += copied
            if offline:
                print("Pi reachable again")
                offline = False
            if copied or failed:
                print(f"{time.strftime('%H:%M:%S')}  +{copied} copied, {failed} to retry, {total} total")
        except (ConnectionError, subprocess.TimeoutExpired) as e:
            _resolved.clear()  # the Pi may have a new IP (e.g. hotspot reconnect): look it up again
            if not offline or not a.watch:
                print(f"Can't reach the Pi ({e}); {'retrying' if a.watch else 'giving up'}")
            offline = True
            if not a.watch:
                sys.exit(1)
        if not a.watch:
            print(f"Done: {total} new photo(s) in {dest}")
            return
        try:
            time.sleep(a.interval)
        except KeyboardInterrupt:
            print(f"\nStopped. {total} photo(s) copied this run.")
            return


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        print("\nStopped.")
