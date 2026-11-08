"""Fail before serving if CUDA, checkpoint loading or real inference is unavailable."""
import argparse,io,json,os,sys,tempfile
from pathlib import Path

def main():
    p=argparse.ArgumentParser();p.add_argument('--cuda-only',action='store_true');p.add_argument('--full',action='store_true');p.add_argument('--experts',action='store_true');p.add_argument('--legacy',action='store_true');args=p.parse_args()
    root=Path(__file__).resolve().parents[1]
    import torch,torchvision
    cuda=torch.cuda.is_available()
    if not cuda and (args.cuda_only or args.experts or args.legacy):raise RuntimeError('CUDA is unavailable in the managed Python environment. Install/update the NVIDIA driver and restart the machine. The legacy GPU model requires CUDA.')
    if cuda and torch.cuda.get_device_capability(0)<(7,5):raise RuntimeError('This installer supports Turing or newer GPUs (compute capability7.5+).')
    # Device availability alone is insufficient: execute kernels from both packages.
    if cuda:
        with torch.inference_mode():
            conv=torch.nn.Conv2d(3,8,3).cuda().half();conv(torch.zeros(1,3,32,32,device='cuda',dtype=torch.float16))
            torchvision.ops.nms(torch.tensor([[0.,0.,1.,1.]],device='cuda'),torch.ones(1,device='cuda'),.5)
            torch.cuda.synchronize()
    result={'cuda_kernels':cuda,'torch':torch.__version__,'torchvision':torchvision.__version__,'cuda':torch.version.cuda,'gpu':torch.cuda.get_device_name(0) if cuda else None,'python':sys.version.split()[0]}
    if args.full:
        from PIL import Image,ImageDraw
        import numpy as np
        sys.path.insert(0,str(root/'python'))
        from process_leaves import ground_mask
        image=Image.new('RGB',(512,512),(110,95,65));draw=ImageDraw.Draw(image)
        draw.ellipse((100,45,400,465),fill=(40,145,55));draw.line((250,80,250,445),fill=(180,200,90),width=4)
        mask=ground_mask(image,root/'models/PlantLeafPipeline/models/segformer-b2-ade20k')
        assert mask.size==image.size and np.isin(np.asarray(mask),[0,255]).all(),'Invalid B2 mask'
        if args.experts:
            from crop_experts import ExpertPredictor
            output=io.BytesIO();image.save(output,format='JPEG')
            prediction=ExpertPredictor(root/'models/CropExperts-ResNet18','cuda').predict_bytes(output.getvalue())
            assert prediction['verdict'] in ('healthy','diseased')
            result['classifier']='crop-experts-cuda'
        elif args.legacy:
            directory=root/'models/LeafCNN-Epoch8-GPU';sys.path.insert(0,str(directory))
            from run_leaf_gpu import LeafGPUModel
            model=LeafGPUModel(directory/'selected-gpu-model.pt',directory/'inference-config.json',directory/'exported-artifact-validation.json')
            with tempfile.TemporaryDirectory(prefix='leaflink-runtime-probe-') as folder:
                photo=Path(folder)/'probe.jpg';image.save(photo,format='JPEG');prediction=model.predict(photo)
            assert prediction['verdict'] in ('healthy','diseased')
            result.update(classifier='epoch8-fp16-cuda',model_sha256=prediction['model_sha256'])
        else:
            from segmented_leaf_model import SegmentedLeafModel
            directory=Path(os.environ.get('SEGMENTED_MODEL_DIR',root/'models/LeafCNN-Field-Checkpoint1'))
            segmentation=Path(os.environ.get('SEGMENTATION_MODEL_DIR',root/'models/SAM2.1-Hiera-Tiny'))
            model=SegmentedLeafModel(directory,segmentation,device=os.environ.get('SEGMENTED_DEVICE','auto'),cpu_threads=4)
            photo=io.BytesIO();image.save(photo,format='JPEG');photo.seek(0);prediction=model.predict(photo)
            assert prediction['verdict'] in ('healthy','diseased')
            result.update(classifier='checkpoint1-sam2',model_sha256=model.settings['model_sha256'],segmentation_sha256=model.settings['segmentation_files']['model.safetensors'],device=prediction.get('device'))
            result['segmentation_checkpoint']=True
        result.update(b2_checkpoint=True,synthetic_inference=True)
    print(json.dumps(result),flush=True)

if __name__=='__main__':
    try:main()
    except Exception as error:
        print(f'Runtime check failed ({type(error).__name__}): {error}',file=sys.stderr)
        if sys.platform=='win32':print('For DLL-load errors install the Microsoft Visual C++ x64 runtime: https://aka.ms/vs/17/release/vc_redist.x64.exe',file=sys.stderr)
        raise SystemExit(1)
