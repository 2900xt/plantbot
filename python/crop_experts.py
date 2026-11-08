"""Frozen ImageNet ResNet-18 + four hard-routed specialist binary heads.

Exactly one expert is selected for every image. No general binary head exists.
"""
import hashlib
import io
import json
from pathlib import Path

import torch
from torch import nn
from torchvision.models import resnet18,ResNet18_Weights
from torchvision.transforms import v2
from PIL import Image,ImageOps

MEAN=[.485,.456,.406]
STD=[.229,.224,.225]

class CropExperts(nn.Module):
    def __init__(self,pretrained=False):
        super().__init__()
        self.backbone=resnet18(weights=ResNet18_Weights.IMAGENET1K_V1 if pretrained else None)
        self.backbone.fc=nn.Identity()
        self.router=nn.Linear(512,4)
        self.experts=nn.ModuleList([nn.Sequential(nn.Linear(512,128),nn.ReLU(),nn.Dropout(.2),nn.Linear(128,1)) for _ in range(4)])
        for parameter in self.backbone.parameters():parameter.requires_grad_(False)
        self.backbone.eval()

    def train(self,mode=True):
        super().train(mode)
        self.backbone.eval()
        return self

    def heads(self,features):
        return self.router(features),torch.cat([expert(features) for expert in self.experts],dim=1)

    def forward(self,images):
        return self.heads(self.backbone(images))

def transform(image,size=224):
    tensor=v2.functional.to_image(image)
    side=min(tensor.shape[-2:])
    tensor=v2.functional.center_crop(tensor,[side,side])
    tensor=v2.functional.resize(tensor,[size,size],antialias=True)
    tensor=v2.functional.to_dtype(tensor,torch.float32,scale=True)
    return v2.functional.normalize(tensor,MEAN,STD)

class ExpertPredictor:
    def __init__(self,directory,device='auto'):
        directory=Path(directory)
        if device not in ('auto','cpu','cuda'):raise ValueError('Invalid expert device')
        self.device=torch.device('cuda' if device=='auto' and torch.cuda.is_available() else 'cpu' if device=='auto' else device)
        if self.device.type=='cuda' and not torch.cuda.is_available():raise RuntimeError('CUDA not available')
        self.config=json.loads((directory/'config.json').read_text())
        if self.config['architecture']!='frozen-resnet18-four-experts' or self.config['experts']!=4 or self.config['general_fallback'] is not False:
            raise ValueError('Expected four experts with no general fallback')
        if self.config['image_size']!=224 or self.config['mean']!=MEAN or self.config['std']!=STD:raise ValueError('Preprocessing mismatch')
        self.fingerprint=hashlib.sha256((directory/'model.pt').read_bytes()).hexdigest()
        if self.fingerprint!=self.config['model_sha256']:raise ValueError('Expert model hash mismatch')
        self.model=CropExperts()
        self.model.load_state_dict(torch.load(directory/'model.pt',map_location='cpu',weights_only=True)['model'],strict=True)
        self.model.to(self.device).eval()
        torch.set_num_threads(4)

    def predict_bytes(self,data):
        with Image.open(io.BytesIO(data)) as raw:
            if raw.width*raw.height>40_000_000 or getattr(raw,'n_frames',1)!=1:raise ValueError('Use a single still image under40megapixels')
            image=ImageOps.exif_transpose(raw).convert('RGB')
            batch=transform(image).unsqueeze(0).to(self.device)
        with torch.inference_mode(),torch.autocast(device_type=self.device.type,dtype=torch.float16,enabled=self.device.type=='cuda'):
            router,logits=self.model(batch)
        router=router.float().softmax(1)[0];index=int(router.argmax())
        score=float(logits.float()[0,index].sigmoid())
        if not torch.isfinite(router).all() or not 0<=score<=1:raise ValueError('Nonfinite expert prediction')
        return dict(verdict='diseased' if score>=.5 else 'healthy',confidence=max(score,1-score),disease_score=score,
            model_version='crop-experts-resnet18-'+self.fingerprint[:16],model_sha256=self.fingerprint,
            score_type='selected_expert_sigmoid',binary_score_type='selected_expert_sigmoid',
            expert_index=index,expert_crops=self.config['expert_crops'][str(index)],router_score=float(router[index]),
            router_probabilities=router.tolist(),device=str(self.device),input_size=[224,224],simulated=False)
