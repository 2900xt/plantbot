"""Standalone CUDA-only inference for a hash-bound selected LeafCNN artifact.

Load LeafGPUModel once and call predict for multiple images. CLI emits one JSON
prediction per image and a ready event to stderr. Does not change the app.
"""
import argparse
import hashlib
import json
from pathlib import Path
import sys
import time


def metadata_settings(metadata,model_hash):
    """Accept the original GPU audit report or a compact delivery config."""
    if 'artifacts' in metadata:
        selected=metadata['artifacts']['selected'];name=metadata['selected_candidate']
        if selected['candidate']!=name or metadata['metrics'][name].get('promotion_eligible') is not True:
            raise ValueError('Expected a validated selected candidate')
        classes=metadata['class_order'];size=metadata['input_size']
        precision=selected['precision'];expected_hash=selected['sha256']
        if name.endswith('_channels_last'):layout='channels_last'
        elif name.endswith('_contiguous'):layout='contiguous'
        else:raise ValueError('Selected candidate does not declare its input layout')
        mean=std=[.5,.5,.5]
        epoch=metadata['epoch_label']
    else:
        classes=metadata['classes'];size=metadata['image_size'];precision=metadata['inference_precision']
        expected_hash=metadata['model_sha256'];layout=metadata['memory_format']
        mean=metadata['mean'];std=metadata['std'];epoch=metadata['epoch']
    if expected_hash!=model_hash:raise ValueError('Model SHA256 differs from metadata')
    if size!=384 or len(classes)!=38 or len(set(classes))!=38:raise ValueError('Expected unchanged 384px /38-class model')
    if any(not isinstance(label,str) for label in classes):raise ValueError('Invalid saved class order')
    if mean!=[.5,.5,.5] or std!=[.5,.5,.5]:raise ValueError('Expected saved mean/std [0.5,0.5,0.5]')
    if precision not in ['torch.float16','torch.float32'] or layout not in ['channels_last','contiguous']:
        raise ValueError('Unsupported saved GPU precision/layout')
    return {'epoch':epoch,'image_size':384,'classes':classes,'mean':mean,'std':std,
            'inference_precision':precision,'memory_format':layout,'model_sha256':model_hash}


def preprocess_image(image_path,settings):
    from PIL import Image,ImageOps
    import torch
    from torchvision.transforms import v2
    with Image.open(image_path) as raw:
        if raw.width*raw.height>40_000_000:raise ValueError('Image exceeds 40 megapixels')
        if getattr(raw,'n_frames',1)!=1:raise ValueError('Use a single still image')
        image=ImageOps.exif_transpose(raw).convert('RGB')
        tensor=v2.functional.to_image(image)
    side=min(tensor.shape[-2:])
    tensor=v2.functional.center_crop(tensor,[side,side])
    tensor=v2.functional.resize(tensor,[384,384],antialias=True)
    tensor=v2.functional.to_dtype(tensor,torch.float32,scale=True)
    return v2.functional.normalize(tensor,settings['mean'],settings['std'])


class LeafGPUModel:
    def __init__(self,model_path,metadata_path,validation_path=None):
        # Bind metadata before deserializing the model.
        model_path=Path(model_path)
        digest=hashlib.sha256(model_path.read_bytes()).hexdigest()
        metadata=json.loads(Path(metadata_path).read_text(encoding='utf8'))
        self.settings=metadata_settings(metadata,digest)
        self.export_validation_supplied=False
        if validation_path:
            validation=json.loads(Path(validation_path).read_text(encoding='utf8'))
            if validation.get('passed') is not True or validation.get('artifact_sha256')!=digest:
                raise ValueError('Post-export validation does not pass for this artifact')
            if validation.get('class_order')!=self.settings['classes'] or validation.get('epoch')!=self.settings['epoch']:
                raise ValueError('Post-export validation class order/epoch mismatch')
            self.export_validation_supplied=True
        import torch
        from torchvision.transforms import v2
        if not torch.cuda.is_available():
            raise RuntimeError('CUDA is required for this GPU artifact. Install a compatible CUDA PyTorch build and run on an NVIDIA GPU.')
        self.torch=torch;self.v2=v2;self.device=torch.device('cuda:0');torch.cuda.set_device(0)
        self.dtype=torch.float16 if self.settings['inference_precision']=='torch.float16' else torch.float32
        self.layout=torch.channels_last if self.settings['memory_format']=='channels_last' else torch.contiguous_format
        torch.backends.cudnn.benchmark=True
        torch.backends.cuda.matmul.allow_tf32=False;torch.backends.cudnn.allow_tf32=False
        started=time.perf_counter()
        self.model=torch.jit.load(str(model_path),map_location=self.device).eval()
        self.load_ms=(time.perf_counter()-started)*1000

    def predict(self,image_path):
        torch=self.torch;started=time.perf_counter()
        tensor=preprocess_image(image_path,self.settings)
        batch=tensor.unsqueeze(0).to(device=self.device,dtype=self.dtype).contiguous(memory_format=self.layout)
        with torch.inference_mode():
            start=torch.cuda.Event(enable_timing=True);end=torch.cuda.Event(enable_timing=True)
            start.record();logits=self.model(batch);end.record();end.synchronize()
            if logits.shape!=(1,38) or not torch.isfinite(logits).all():raise ValueError('Invalid GPU model output')
            probs=logits.float().softmax(1)[0].cpu()
        values,indices=probs.topk(3);classes=self.settings['classes'];top=int(indices[0])
        return {'image':str(image_path),'epoch':self.settings['epoch'],'model_sha256':self.settings['model_sha256'],
                'device':torch.cuda.get_device_name(0),'precision':self.settings['inference_precision'],'input_size':384,
                'class':classes[top],'verdict':'healthy' if classes[top].endswith('_healthy') else 'diseased',
                'confidence':float(values[0]),'top3':[{'label':classes[int(i)],'probability':float(v)} for v,i in zip(values,indices)],
                'classes':classes,'probabilities':probs.tolist(),'forward_ms':start.elapsed_time(end),
                'request_ms':(time.perf_counter()-started)*1000,'post_export_validation_supplied':self.export_validation_supplied}


def main():
    p=argparse.ArgumentParser(description=__doc__)
    p.add_argument('--model',type=Path,required=True)
    p.add_argument('--metadata',type=Path,required=True,help='gpu-benchmark.json or compact inference-config.json')
    p.add_argument('--validation',type=Path,help='Recommended: exported-artifact-validation.json PASS for same SHA')
    p.add_argument('--image',type=Path,nargs='+',required=True)
    p.add_argument('--write-config',type=Path,help='Write compact hash-bound inference settings for sharing')
    a=p.parse_args()
    predictor=LeafGPUModel(a.model,a.metadata,a.validation)
    if a.write_config:
        a.write_config.parent.mkdir(parents=True,exist_ok=True)
        a.write_config.write_text(json.dumps(predictor.settings,indent=2),encoding='utf8')
    print(json.dumps({'event':'ready','model_load_ms':predictor.load_ms,'model_sha256':predictor.settings['model_sha256'],
                      'precision':predictor.settings['inference_precision'],'memory_format':predictor.settings['memory_format']}),file=sys.stderr,flush=True)
    for image_path in a.image:print(json.dumps(predictor.predict(image_path)),flush=True)


if __name__=='__main__':main()
