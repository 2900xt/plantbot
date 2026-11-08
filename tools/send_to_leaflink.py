#!/usr/bin/env python3
"""Send rover photos from Pictures_Test/ to the LeafLink leaf-disease model, each exactly once.

    py send_to_leaflink.py                    # watch Pictures_Test next to this script
    py send_to_leaflink.py --once             # send what's there, wait for results, exit
    py send_to_leaflink.py --photos D:/pics --leaflink C:/path/to/LeafLink-main

Runs on the GPU laptop next to LeafLink and tools/pull_pictures.py --watch:
    Pi (wheel_capture) -> pull_pictures.py -> Pictures_Test/<session>/NNNN.jpg -> this -> LeafLink

* Reads LeafLink's address and API token from its .env.field (never printed).
* Only sends finished *.jpg files (pull_pictures.py writes .part files first).
* Idempotency-Key = <session>_<file>, and sent photos are remembered in
  Pictures_Test/.leaflink_sent.json, so restarts never create duplicates.
* LeafLink accepts at most 20 uploads a minute; the robot takes 60. Photos queue
  up and go out every ~3 s, oldest first, so a long session takes a while to clear.
* Prints LeafLink's verdict for each photo as the model finishes it.
Standard library only.
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import time
import urllib.error
import urllib.request
import uuid
from pathlib import Path

SEND_GAP_S = 3.2          # 20 uploads/min limit on POST /api/captures
PENDING = {"queued", "uploading", "splitting", "processing"}
STATE_NAME = ".leaflink_sent.json"


def read_env(path: Path) -> dict[str, str]:
    env = {}
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if line and not line.startswith("#") and "=" in line:
            k, v = line.split("=", 1)
            env[k.strip()] = v.strip().strip('"').strip("'")
    return env


class LeafLink:
    def __init__(self, base: str, token: str):
        self.base, self.auth = base.rstrip("/"), f"Bearer {token}"

    def _req(self, method, path, body=None, headers=None, timeout=30):
        req = urllib.request.Request(self.base + path, data=body, method=method,
                                     headers={"Authorization": self.auth, **(headers or {})})
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return json.loads(r.read() or b"null")

    def healthy(self) -> bool:
        try:
            with urllib.request.urlopen(self.base + "/api/health", timeout=5) as r:
                return r.status == 200
        except OSError:
            return False

    def upload(self, jpg: Path, device: str, key: str) -> dict:
        boundary = uuid.uuid4().hex
        parts = [
            f'--{boundary}\r\nContent-Disposition: form-data; name="device"\r\n\r\n{device}\r\n'.encode(),
            (f'--{boundary}\r\nContent-Disposition: form-data; name="image"; filename="{jpg.name}"\r\n'
             f"Content-Type: image/jpeg\r\n\r\n").encode() + jpg.read_bytes() + b"\r\n",
            f"--{boundary}--\r\n".encode(),
        ]
        return self._req("POST", "/api/captures", b"".join(parts), {
            "Content-Type": f"multipart/form-data; boundary={boundary}", "Idempotency-Key": key}, timeout=60)

    def captures(self) -> list[dict]:
        return self._req("GET", "/api/captures")


def describe(row: dict) -> str:
    res = row.get("result") or {}
    if res.get("verdict"):
        sim = " (SIMULATED model)" if res.get("simulated") else ""
        return (f"{res['verdict'].upper()}{sim}: {res.get('diseased_count', 0)} diseased / "
                f"{res.get('leaf_count', 0)} leaves")
    msg = row["status"].replace("_", " ")
    return f"{msg}: {row['error']}" if row.get("error") else msg


def main():
    here = Path(__file__).resolve().parent
    ap = argparse.ArgumentParser()
    ap.add_argument("--photos", default=str(here / "Pictures_Test"), help="folder pull_pictures.py fills")
    ap.add_argument("--leaflink", default=str(Path.home() / "Downloads/LeafLink-main/LeafLink-main"),
                    help="LeafLink folder (holds .env.field)")
    ap.add_argument("--device", default="rover-1", help="device name shown in LeafLink")
    ap.add_argument("--once", action="store_true", help="send everything, wait for results, then exit")
    a = ap.parse_args()

    photos = Path(a.photos)
    env_file = Path(a.leaflink) / ".env.field"
    if not env_file.exists():
        sys.exit(f"Can't find {env_file}. Point --leaflink at the LeafLink folder.")
    env = read_env(env_file)
    host = env.get("HOST", "127.0.0.1")
    host = "127.0.0.1" if host in ("0.0.0.0", "::", "") else host
    ll = LeafLink(f"http://{host}:{env.get('PORT', '8080')}", env["API_TOKEN"])

    state_file = photos / STATE_NAME
    try:
        sent: dict[str, dict] = json.loads(state_file.read_text())
    except (OSError, ValueError):
        sent = {}

    def save():
        tmp = state_file.with_suffix(".tmp")
        tmp.write_text(json.dumps(sent, indent=1))
        os.replace(tmp, state_file)

    print(f"{photos}  ->  LeafLink {ll.base} as '{a.device}'")
    while not ll.healthy():
        print("Waiting for LeafLink to start (run Start-LeafLink.cmd)...")
        time.sleep(5)
    print("LeafLink is up.")

    last_send, down = 0.0, False
    while True:
        todo = sorted(str(p.relative_to(photos)).replace("\\", "/") for p in photos.glob("*/*.jpg")
                      if str(p.relative_to(photos)).replace("\\", "/") not in sent)
        waiting = [rel for rel, s in sent.items() if s.get("status") in PENDING]
        try:
            if todo and time.monotonic() - last_send >= SEND_GAP_S:
                rel = todo[0]
                key = rel.replace("/", "_").replace(".jpg", "")
                try:
                    row = ll.upload(photos / rel, a.device, key)
                    sent[rel] = {"id": row["id"], "status": row["status"]}
                    save()
                    print(f"sent   {rel}  (#{row.get('number')}, {len(todo) - 1} queued)")
                except urllib.error.HTTPError as e:
                    if e.code == 429:
                        retry = float(e.headers.get("Retry-After") or 10)
                        print(f"LeafLink rate limit; waiting {retry:.0f} s")
                        last_send = time.monotonic() + retry - SEND_GAP_S
                        continue
                    detail = e.read().decode(errors="replace")[:200]
                    if 400 <= e.code < 500:  # bad image etc.: don't retry forever
                        sent[rel] = {"id": None, "status": "rejected", "error": f"{e.code} {detail}"}
                        save()
                        print(f"REJECTED {rel}: {e.code} {detail}")
                    else:
                        print(f"error sending {rel}: {e.code} {detail} (will retry)")
                last_send = time.monotonic()
            if waiting:
                by_id = {r["id"]: r for r in ll.captures()}
                for rel in waiting:
                    row = by_id.get(sent[rel]["id"])
                    if row and row["status"] not in PENDING:
                        sent[rel] = {"id": row["id"], "status": row["status"], "result": row.get("result")}
                        print(f"result {rel}: {describe(row)}")
                        save()
            if down:
                print("LeafLink reachable again")
                down = False
        except (urllib.error.URLError, OSError, TimeoutError) as e:
            if not down:
                print(f"Can't reach LeafLink ({e}); retrying")
            down = True
            time.sleep(5)
        if a.once and not todo and not waiting:
            done = [s for s in sent.values() if s.get("status") not in PENDING]
            print(f"Done: {len(done)} photo(s) have results in LeafLink.")
            return
        time.sleep(1)


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        print("\nStopped. Unsent photos will go next time.")
