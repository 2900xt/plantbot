"""Resident JSON-lines adapter for the routed crop expert package."""
import base64
import json
from pathlib import Path
import sys

directory = Path(sys.argv[1]).resolve()
device = sys.argv[2]
threads = int(sys.argv[3])
predictor = None
for line in sys.stdin:
    try:
        payload = json.loads(line)
        if predictor is None:
            import torch
            torch.set_num_threads(threads)
            from crop_experts import ExpertPredictor
            predictor = ExpertPredictor(directory, device=device)
        image = base64.b64decode(payload['image'], validate=True)
        output = predictor.predict_bytes(image)
        print(json.dumps(output, allow_nan=False), flush=True)
    except Exception:
        # No uploaded bytes, credentials or filesystem details in API errors.
        print(json.dumps({'error': 'Crop expert model unavailable', 'code': 'MODEL_UNAVAILABLE'}), flush=True)
