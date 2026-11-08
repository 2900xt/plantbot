"""Resident, sequential extraction bridge. Models and imports survive between frames."""
import json
import sys
from process_leaves import process

for line in sys.stdin:
    try:
        print(json.dumps(process(json.loads(line))), flush=True)
    except Exception:
        # Keep protocol and credentials private. Node rejects and retries the job.
        print(json.dumps({"error": "Leaf extraction failed; check ground model, device and Python dependencies"}), flush=True)
