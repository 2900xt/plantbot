import contextlib
import hashlib
import json
from pathlib import Path
import time


def digest(path):
    value=hashlib.sha256()
    with Path(path).open('rb') as source:
        for chunk in iter(lambda:source.read(1024*1024),b''):value.update(chunk)
    return value.hexdigest()


def load_settings(model_dir,segmentation_dir):
    model_dir=Path(model_dir);segmentation_dir=Path(segmentation_dir)
    settings=json.loads((model_dir/'inference-config.json').read_text(encoding='utf8'))
    classes=settings['classes']
    if settings.get('model_family')!='leaf-cnn-checkpoint1-sam2' or settings.get('checkpoint_epoch')!=1:
        raise ValueError('Expected checkpoint 1 segmented classifier')
    if settings.get('image_size')!=384 or len(classes)!=38 or len(set(classes))!=38 or any(not isinstance(c,str) for c in classes):
        raise ValueError('Invalid classifier dimensions or class order')
    if settings.get('mean')!=[.5]*3 or settings.get('std')!=[.5]*3:
        raise ValueError('Invalid classifier normalization')
    if digest(model_dir/'model.pt')!=settings['model_sha256']:raise ValueError('Classifier checksum mismatch')
    names={'model.safetensors','config.json','preprocessor_config.json','processor_config.json'}
    if set(settings['segmentation_files'])!=names:raise ValueError('Invalid segmentation asset manifest')
    for name,expected in settings['segmentation_files'].items():
        if digest(segmentation_dir/name)!=expected:raise ValueError('Segmentation checksum mismatch: '+name)
    return settings


def create_classifier():
    from torch import nn
    class LeafCNN(nn.Module):
        def __init__(self):
            super().__init__();layers=[];channels=3
            for width in (32,64,128,256):
                for _ in range(2):
                    layers.extend([nn.Sequential(nn.Conv2d(channels,width,3,padding=1,bias=True),nn.ReLU()),nn.Identity(),nn.Identity()]);channels=width
                layers.append(nn.MaxPool2d(2))
            self.features=nn.Sequential(*layers)
            self.head=nn.Sequential(nn.AdaptiveAvgPool2d(1),nn.Flatten(),nn.Linear(256,128),nn.ReLU(),nn.Dropout(.4),nn.Linear(128,38))
        def forward(self,x):return self.head(self.features(x))
    return LeafCNN()


class SegmentedLeafModel:
    def __init__(self,model_dir,segmentation_dir,device='auto',cpu_threads=4):
        self.settings=load_settings(model_dir,segmentation_dir)
        import torch
        from transformers import Sam2Model,Sam2Processor
        from torchvision.transforms import v2
        if device not in ('auto','cpu','cuda'):raise ValueError('Invalid classifier device')
        if not 1<=int(cpu_threads)<=32:raise ValueError('Invalid CPU thread count')
        self.torch=torch;self.v2=v2;torch.set_num_threads(int(cpu_threads))
        self.device='cuda' if device=='auto' and torch.cuda.is_available() else ('cpu' if device=='auto' else device)
        if self.device=='cuda' and not torch.cuda.is_available():raise RuntimeError('CUDA is unavailable')
        torch.backends.cudnn.benchmark=True
        torch.backends.cuda.matmul.allow_tf32=False;torch.backends.cudnn.allow_tf32=False
        state=torch.load(Path(model_dir)/'model.pt',map_location='cpu',weights_only=True)
        if state['epoch']!=1 or state['classes']!=self.settings['classes']:raise ValueError('Checkpoint class order mismatch')
        self.model=create_classifier();self.model.load_state_dict(state['model'],strict=True)
        self.model=self.model.to(self.device).to(memory_format=torch.channels_last).eval()
        self.segmenter=Sam2Model.from_pretrained(str(segmentation_dir),local_files_only=True).to(self.device).eval()
        self.processor=Sam2Processor.from_pretrained(str(segmentation_dir),local_files_only=True)
        self.version='leaf-cnn-checkpoint1-sam2-'+self.settings['model_sha256'][:12]+'-'+self.settings['segmentation_files']['model.safetensors'][:8]

    def segment(self,image,context=None):
        import numpy as np
        from PIL import Image
        context=context or {};w,h=image.size
        if 'box' in context:
            box=np.asarray(context['box'],dtype=float)
        elif 'leaf_bounds' in context and 'crop_bounds' in context:
            bounds=np.asarray(context['leaf_bounds'],dtype=float);crop=np.asarray(context['crop_bounds'],dtype=float)
            if bounds.shape!=(4,) or crop.shape!=(4,) or not np.isfinite(bounds).all() or not np.isfinite(crop).all() or crop[2]<=crop[0] or crop[3]<=crop[1]:raise ValueError('Invalid candidate bounds')
            box=(bounds-np.array([crop[0],crop[1],crop[0],crop[1]]))*np.array([w,h,w,h])/np.array([crop[2]-crop[0],crop[3]-crop[1],crop[2]-crop[0],crop[3]-crop[1]])
        else:box=np.array([8,8,w-8,h-8],dtype=float)
        if box.shape!=(4,) or not np.isfinite(box).all():raise ValueError('Invalid segmentation box')
        box=np.clip(box,[0,0,0,0],[w-1,h-1,w,h]).tolist()
        if box[2]<=box[0] or box[3]<=box[1]:box=[8,8,w-8,h-8]
        values=self.processor(images=image,input_boxes=[[box]],return_tensors='pt').to(self.device)
        with self.torch.inference_mode():prediction=self.segmenter(**values,multimask_output=False)
        masks=self.processor.post_process_masks(prediction.pred_masks.cpu(),values['original_sizes'].cpu())[0]
        mask=masks[0,0].numpy().astype(bool);fraction=float(mask.mean());quality=float(prediction.iou_scores.flatten()[0])
        accepted=bool(.03<=fraction<=.98 and np.isfinite(quality))
        pixels=np.asarray(image).copy()
        if accepted:pixels[~mask]=128
        return Image.fromarray(pixels),dict(accepted=accepted,mask_fraction=fraction,quality=quality if np.isfinite(quality) else None)

    def predict(self,image_path,context=None):
        from PIL import Image,ImageOps
        started=time.perf_counter();torch=self.torch;v2=self.v2
        with Image.open(image_path) as raw:
            if raw.width*raw.height>40_000_000 or getattr(raw,'n_frames',1)!=1:raise ValueError('Use a single image of at most 40 megapixels')
            image=ImageOps.exif_transpose(raw).convert('RGB')
        image,segmentation=self.segment(image,context)
        tensor=v2.functional.to_image(image);side=min(tensor.shape[-2:])
        tensor=v2.functional.center_crop(tensor,[side,side]);tensor=v2.functional.resize(tensor,[384,384],antialias=True)
        tensor=v2.functional.normalize(v2.functional.to_dtype(tensor,torch.float32,scale=True),[.5]*3,[.5]*3)
        x=tensor.unsqueeze(0).to(self.device).contiguous(memory_format=torch.channels_last)
        precision=torch.autocast('cuda',dtype=torch.float16) if self.device=='cuda' else contextlib.nullcontext()
        with torch.inference_mode(),precision:logits=self.model(x)
        if logits.shape!=(1,38) or not torch.isfinite(logits).all():raise ValueError('Invalid classifier output')
        probabilities=logits.float().softmax(1)[0].cpu();values,indices=probabilities.topk(3);classes=self.settings['classes'];top=int(probabilities.argmax())
        return dict(verdict='healthy' if classes[top].endswith('_healthy') else 'diseased',confidence=float(probabilities[top]),
            model_version=self.version,class_name=classes[top],class_index=top,input_size=[384,384],score_type='winning_class_softmax',
            classes=classes,probabilities=probabilities.tolist(),top_predictions=[dict(class_name=classes[int(i)],confidence=float(v)) for v,i in zip(values,indices)],
            segmentation=segmentation,device=self.device,request_ms=(time.perf_counter()-started)*1000)
