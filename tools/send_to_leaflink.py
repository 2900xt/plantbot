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
* When a recording stops, the Pi writes <session>/session.json. Once that file is
  here and every photo it lists has a result, this writes <session>/report.html
  (summary + every photo's verdict) and opens it and LeafLink in the browser.
  Once per session; delete report.html to have it rebuilt. --no-open: just write it.
Standard library only.
"""
from __future__ import annotations

import argparse
import html
import json
import os
import sys
import time
import urllib.error
import urllib.request
import uuid
import webbrowser
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


REPORT_CSS = """
:root{--bg:#f7f6f2;--card:#fff;--ink:#1d2a22;--muted:#5d6b62;--line:#dcd9cf;--bad:#b3261e;--bad-bg:#fbe9e7;
--ok:#2e6b3a;--ok-bg:#e6f2e7;--na:#6b6b6b;--na-bg:#eeeeec}
@media(prefers-color-scheme:dark){:root{--bg:#141815;--card:#1d221e;--ink:#e6ebe6;--muted:#9aa69d;--line:#2f3631;
--bad:#ff8a80;--bad-bg:#3a1f1d;--ok:#8fd19e;--ok-bg:#1d3322;--na:#b0b0b0;--na-bg:#2a2d2b}}
*{box-sizing:border-box}body{margin:0;background:var(--bg);color:var(--ink);font:15px/1.45 system-ui,'Segoe UI',sans-serif}
main{max-width:1180px;margin:0 auto;padding:28px 16px 48px}h1{font-size:24px;margin:0 0 4px}
.sub{color:var(--muted);margin:0 0 20px}.sub a{color:inherit}
.stats{display:grid;grid-template-columns:repeat(auto-fit,minmax(140px,1fr));gap:10px;margin-bottom:12px}
.stat{background:var(--card);border:1px solid var(--line);border-radius:10px;padding:12px 14px}
.stat b{display:block;font-size:26px;line-height:1.1}.stat span{color:var(--muted);font-size:13px}
.stat.bad b{color:var(--bad)}.stat.ok b{color:var(--ok)}
.note{color:var(--muted);font-size:13px;margin:0 0 22px}
.grid{display:grid;grid-template-columns:repeat(auto-fill,minmax(250px,1fr));gap:14px}
.card{background:var(--card);border:1px solid var(--line);border-radius:10px;overflow:hidden;display:flex;flex-direction:column}
.card img{width:100%;aspect-ratio:16/9;object-fit:cover;display:block;background:var(--line)}
.meta{padding:10px 12px 12px;display:flex;flex-direction:column;gap:4px}
.row{display:flex;justify-content:space-between;align-items:center;gap:8px}
.badge{font-size:12px;font-weight:600;padding:2px 8px;border-radius:999px;background:var(--na-bg);color:var(--na)}
.badge.diseased{background:var(--bad-bg);color:var(--bad)}.badge.healthy{background:var(--ok-bg);color:var(--ok)}
.name{font-family:ui-monospace,Consolas,monospace;font-size:13px}.leaves{color:var(--muted);font-size:13px}
.card a{color:var(--muted);font-size:12px}
"""


def build_report(folder: Path, marker: dict, rows: list[tuple[str, dict]], leaflink_base: str) -> Path:
    """Write <session>/report.html from the per-photo LeafLink results."""
    counts = {"diseased": 0, "healthy": 0, "other": 0}
    cards = []
    for name, s in rows:
        res = s.get("result") or {}
        verdict = res.get("verdict")
        kind = verdict if verdict in ("diseased", "healthy") else "other"
        counts[kind] += 1
        label = {"diseased": "Diseased", "healthy": "Healthy"}.get(kind) or {
            "no_candidates": "No leaves found", "retake_blurry": "Too blurry",
            "rejected": "Rejected"}.get(s.get("status"), (s.get("status") or "?").replace("_", " ").capitalize())
        if res.get("simulated"):
            label += " (simulated)"
        leaves = (f"{res.get('diseased_count', 0)} diseased of {res.get('leaf_count', 0)} leaves"
                  if verdict else "&nbsp;")
        link = (f'<a href="{leaflink_base}/#images/{html.escape(s["id"])}" target="_blank">Open in LeafLink</a>'
                if s.get("id") else "")
        cards.append(f'<article class="card"><a href="{html.escape(name)}" target="_blank">'
                     f'<img src="{html.escape(name)}" alt="Photo {html.escape(name)}" loading="lazy"></a>'
                     f'<div class="meta"><div class="row"><span class="name">{html.escape(name)}</span>'
                     f'<span class="badge {kind}">{html.escape(label)}</span></div>'
                     f'<span class="leaves">{leaves}</span>{link}</div></article>')
    judged = counts["diseased"] + counts["healthy"]
    pct = f"{round(100 * counts['diseased'] / judged)}%" if judged else "n/a"
    started = marker.get("started", folder.name)
    skipped = marker.get("skipped") or 0
    page = f"""<!doctype html><html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1"><title>Session {html.escape(folder.name)}</title>
<style>{REPORT_CSS}</style></head><body><main>
<h1>Plant scan: {html.escape(folder.name.replace('_', ' '))}</h1>
<p class="sub">Recorded {html.escape(str(started))} to {html.escape(str(marker.get('stopped', '?')))} &middot;
{len(rows)} photos{f' &middot; {skipped} skipped (camera dropped out)' if skipped else ''} &middot;
model results from <a href="{leaflink_base}/#images" target="_blank">LeafLink</a></p>
<section class="stats">
<div class="stat bad"><b>{counts['diseased']}</b><span>photos with disease</span></div>
<div class="stat ok"><b>{counts['healthy']}</b><span>healthy photos</span></div>
<div class="stat"><b>{counts['other']}</b><span>no verdict (no leaves, blurry)</span></div>
<div class="stat"><b>{pct}</b><span>of judged photos diseased</span></div>
</section>
<p class="note">Verdicts are only meaningful for photos aimed at plants: the model can find
leaf-like shapes in anything.</p>
<section class="grid">{''.join(cards)}</section>
</main></body></html>"""
    out = folder / "report.html"
    tmp = folder / ".report.html.tmp"
    tmp.write_text(page, encoding="utf-8")
    os.replace(tmp, out)
    return out


def finished_sessions(photos: Path, sent: dict) -> list[tuple[Path, dict, list]]:
    """Sessions whose recording stopped (session.json), all photos here and all classified, no report yet."""
    ready = []
    for marker_file in sorted(photos.glob("*/session.json")):
        folder = marker_file.parent
        if (folder / "report.html").exists():
            continue
        try:
            marker = json.loads(marker_file.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        jpgs = sorted(p.name for p in folder.glob("*.jpg"))
        if len(jpgs) < int(marker.get("photos", 0)):
            continue  # photos still being copied from the Pi
        rels = [f"{folder.name}/{n}" for n in jpgs]
        if any(r not in sent or sent[r].get("status") in PENDING for r in rels):
            continue  # still being sent or classified
        ready.append((folder, marker, [(n, sent[f"{folder.name}/{n}"]) for n in jpgs]))
    return ready


def main():
    here = Path(__file__).resolve().parent
    ap = argparse.ArgumentParser()
    ap.add_argument("--photos", default=str(here / "Pictures_Test"), help="folder pull_pictures.py fills")
    # LeafLink folder holding .env.field: the repo's leaflink/ when this runs from tools/,
    # else a plantbot clone in Downloads, else the old standalone LeafLink download.
    candidates = [here.parent / "leaflink", Path.home() / "Downloads/plantbot/leaflink",
                  Path.home() / "Downloads/LeafLink-main/LeafLink-main"]
    default_ll = next((c for c in candidates if (c / ".env.field").exists()), candidates[1])
    ap.add_argument("--leaflink", default=str(default_ll), help="LeafLink folder (holds .env.field)")
    ap.add_argument("--device", default="rover-1", help="device name shown in LeafLink")
    ap.add_argument("--once", action="store_true", help="send everything, wait for results, then exit")
    ap.add_argument("--no-open", action="store_true", help="write session reports but don't open the browser")
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
            for folder, marker, rows in finished_sessions(photos, sent):
                report = build_report(folder, marker, rows, ll.base)
                print(f"REPORT {folder.name}: {len(rows)} photos -> {report}")
                if not a.no_open:
                    webbrowser.open(report.as_uri())
                    webbrowser.open(f"{ll.base}/#images")
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
