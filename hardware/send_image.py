#!/usr/bin/env python3
"""Raspberry Pi uploader: stdlib only; stable retries and optional verdict polling."""
import argparse
import json
import mimetypes
import os
import pathlib
import sys
import time
import urllib.error
import urllib.request
import uuid


def request(url, token, *, body=None, headers=None):
    req = urllib.request.Request(url, data=body, headers={"Authorization": f"Bearer {token}", **(headers or {})})
    with urllib.request.urlopen(req, timeout=45) as response:
        return json.load(response)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("image", type=pathlib.Path)
    parser.add_argument("--server", default=os.getenv("LEAFLINK_URL"))
    parser.add_argument("--device", default="raspberry-pi")
    parser.add_argument("--key", help="Persist/reuse this capture ID if retrying after a reboot")
    parser.add_argument("--wait", action="store_true", help="Poll up to 120 seconds for a result")
    parser.add_argument("--ground-mask", type=pathlib.Path, help="Optional aligned binary PNG from SegFormer-B2 (0 keep, 1/255 ignore)")
    parser.add_argument('--lat',type=float)
    parser.add_argument('--lon',type=float)
    parser.add_argument('--captured-at',help='ISO capture time with timezone; server receipt time is recorded separately')
    args = parser.parse_args()
    token = os.getenv("LEAFLINK_TOKEN")
    if not args.server or not token:
        parser.error("Set LEAFLINK_URL and LEAFLINK_TOKEN (or use --server)")
    data = args.image.read_bytes()
    if len(data) > 12 * 1024 * 1024:
        parser.error("Image exceeds 12 MB")
    key = args.key or str(uuid.uuid4())
    metadata={}
    if args.lat is not None or args.lon is not None:
        if args.lat is None or args.lon is None or not(-85<=args.lat<=85 and -180<=args.lon<=180):
            parser.error('Supply both --lat (-85 to 85) and --lon (-180 to 180)')
        metadata['location']={'lat':args.lat,'lon':args.lon}
    if args.captured_at:
        metadata['captured_at']=args.captured_at
    print(f"Capture idempotency key: {key}", file=sys.stderr)
    boundary = uuid.uuid4().hex
    mime = mimetypes.guess_type(str(args.image))[0] or "application/octet-stream"
    body = (
        f'--{boundary}\r\nContent-Disposition: form-data; name="device"\r\n\r\n{args.device}\r\n'
        f'--{boundary}\r\nContent-Disposition: form-data; name="image"; filename="capture"\r\n'
        f'Content-Type: {mime}\r\n\r\n'
    ).encode() + data + b"\r\n"
    if args.ground_mask:
        mask = args.ground_mask.read_bytes()
        if len(mask) > 12 * 1024 * 1024:
            parser.error("Ground mask exceeds 12 MB")
        body += (f'--{boundary}\r\nContent-Disposition: form-data; name="ground_mask"; filename="mask.png"\r\n'
                 f'Content-Type: image/png\r\n\r\n').encode() + mask + b"\r\n"
    if metadata:
        body+=(f'--{boundary}\r\nContent-Disposition: form-data; name="metadata"\r\n\r\n'+json.dumps(metadata)+'\r\n').encode()
    body += f"--{boundary}--\r\n".encode()
    base = args.server.rstrip("/")
    row = None
    for attempt in range(4):
        try:
            row = request(base + "/api/captures", token, body=body, headers={
                "Content-Type": f"multipart/form-data; boundary={boundary}", "Idempotency-Key": key})
            break
        except urllib.error.HTTPError as error:
            if error.code < 500 and error.code != 429:
                raise SystemExit(f"Upload rejected (HTTP {error.code}): {error.read().decode()}") from error
            if attempt == 3:
                raise SystemExit(f"Upload failed (HTTP {error.code}); retry with --key {key}") from error
        except (urllib.error.URLError, TimeoutError, OSError) as error:
            if attempt == 3:
                raise SystemExit(f"Upload failed; retry with --key {key}") from error
        time.sleep(2 ** attempt)
    if args.wait:
        deadline = time.monotonic() + 240
        while row["status"] not in ("complete", "failed", "awaiting_model", "awaiting_mask", "no_candidates", "retake_blurry") and time.monotonic() < deadline:
            time.sleep(2)
            row = request(base + "/api/captures/" + row["id"], token)
    print(json.dumps(row, indent=2))
    return 1 if row["status"] == "failed" else 0


if __name__ == "__main__":
    raise SystemExit(main())
