import base64
import io
import json
import sys
from segmented_leaf_model import SegmentedLeafModel

predictor=None
for line in sys.stdin:
    try:
        payload=json.loads(line)
        if predictor is None:predictor=SegmentedLeafModel(sys.argv[1],sys.argv[2],sys.argv[3],int(sys.argv[4]))
        image=io.BytesIO(base64.b64decode(payload['image'],validate=True))
        result=predictor.predict(image,payload.get('context'))
        print(json.dumps(result),flush=True)
    except Exception as error:
        print(f'{type(error).__name__}: {error}',file=sys.stderr,flush=True)
        print(json.dumps(dict(error='Segmented checkpoint 1 unavailable. Verify model files and runtime dependencies.',code='MODEL_UNAVAILABLE')),flush=True)
