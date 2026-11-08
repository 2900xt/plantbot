"""Forward live NDJSON VOC/location measurements to LeafLink. Standard library only."""
import argparse
from datetime import datetime, timezone
import json
import os
import sys
import time
import urllib.error
import urllib.request
import uuid

def send(origin, token, row, default_device):
    row=dict(row)
    kind=row.pop('kind','voc')
    if kind not in {'voc','location'}:
        raise ValueError('kind must be voc or location')
    row.setdefault('device',default_device)
    row.setdefault('measured_at',datetime.now(timezone.utc).isoformat())
    if kind=='voc':
        row.setdefault('sample_id',str(uuid.uuid4()))
    payload=json.dumps(row,allow_nan=False).encode()
    request=urllib.request.Request(origin.rstrip('/')+'/api/telemetry/'+kind,data=payload,
        headers={'Authorization':'Bearer '+token,'Content-Type':'application/json'},method='POST')
    for attempt in range(6):
        try:
            with urllib.request.urlopen(request,timeout=30) as response:
                return json.load(response)
        except urllib.error.HTTPError as error:
            if error.code not in {429,500,502,503,504}:
                raise RuntimeError('Telemetry rejected: '+error.read(4096).decode(errors='replace')) from None
            if attempt==5:
                raise RuntimeError('Server remains unavailable; replay the same timestamp/sample_id') from None
            time.sleep(min(60,max(2**attempt,int(error.headers.get('Retry-After','1')))))
        except (urllib.error.URLError,TimeoutError):
            if attempt==5:
                raise RuntimeError('Connection failed; replay the same timestamp/sample_id') from None
            time.sleep(2**attempt)

def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--url',default=os.getenv('LEAFLINK_URL','http://localhost:8080'))
    parser.add_argument('--device',default='field-sensor')
    parser.add_argument('--input',help='NDJSON file; default reads a live stream from stdin')
    args=parser.parse_args()
    token=os.getenv('LEAFLINK_TOKEN')
    if not token:
        parser.error('Set LEAFLINK_TOKEN in the environment')
    source=open(args.input,encoding='utf8') if args.input else sys.stdin
    try:
        for number,line in enumerate(source,1):
            if not line.strip():
                continue
            try:
                row=json.loads(line)
                print(json.dumps(send(args.url,token,row,args.device)),flush=True)
            except Exception as error:
                print(f'Stopped at input line {number}: {error}',file=sys.stderr)
                return 1
    finally:
        if args.input:
            source.close()
    return 0

if __name__=='__main__':
    raise SystemExit(main())
