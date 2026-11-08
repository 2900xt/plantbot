"""Resident JSON-lines bridge to the supplied, hash-bound epoch-8 runner."""
import base64
import json
import sys
import tempfile
from pathlib import Path

directory=Path(sys.argv[1]).resolve()
sys.path.insert(0,str(directory))
predictor=None
for line in sys.stdin:
    try:
        payload=json.loads(line)
        if predictor is None:
            from run_leaf_gpu import LeafGPUModel
            predictor=LeafGPUModel(directory/'selected-gpu-model.pt',directory/'inference-config.json',directory/'exported-artifact-validation.json')
        with tempfile.TemporaryDirectory(prefix='leaf-crop-') as temp:
            photo=Path(temp)/'leaf.jpg'
            photo.write_bytes(base64.b64decode(payload['image'],validate=True))
            result=predictor.predict(photo)
        output={'verdict':result['verdict'],'confidence':result['confidence'],
            'model_version':'leaf-cnn-epoch8-fp16-'+result['model_sha256'][:12],
            'class_name':result['class'],'class_index':result['classes'].index(result['class']),
            'input_size':[384,384],'score_type':'winning_class_softmax',
            'top_predictions':[{'class_name':p['label'],'confidence':p['probability']} for p in result['top3']]}
        print(json.dumps(output),flush=True)
    except Exception as error:
        # No filesystem paths, uploaded images or credentials in API responses.
        print(json.dumps({'error':'Epoch-8 CUDA model unavailable. Check the bundled model, CUDA PyTorch, torchvision and an accessible CUDA GPU.',
            'code':'MODEL_UNAVAILABLE'}),flush=True)
