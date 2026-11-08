"""Secondary classifier-only binary test on labeled leaf-region crops.

This does not stand in for the full-scene B2/splitter test.
"""
import hashlib,json,sys,time
from pathlib import Path
from PIL import Image
root=Path(sys.argv[1]);out=Path(sys.argv[2]);model_dir=Path(sys.argv[3])
sys.path.insert(0,str(model_dir))
from run_leaf_gpu import LeafGPUModel
manifest=json.loads((root/'manifest.json').read_text())
out.mkdir(parents=True,exist_ok=True);(out/'crops').mkdir(exist_ok=True)
predictor=LeafGPUModel(model_dir/'selected-gpu-model.pt',model_dir/'inference-config.json',model_dir/'exported-artifact-validation.json')
rows=[]
for scene in manifest['scenes']:
    with Image.open(root/scene['frame']) as raw:image=raw.convert('RGB')
    for index,obj in enumerate(scene['objects'],1):
        a,b,c,d=obj['frame_box'];side=min(image.width,image.height,round(max(c-a,d-b)*1.2))
        x=max(0,min(round((a+c-side)/2),image.width-side));y=max(0,min(round((b+d-side)/2),image.height-side))
        crop=image.crop((x,y,x+side,y+side)).resize((384,384),Image.Resampling.LANCZOS)
        name=f'{scene["id"]}-leaf-{index:02d}.jpg';photo=out/'crops'/name;crop.save(photo,quality=95)
        pred=predictor.predict(photo)
        row={'scene_id':scene['id'],'leaf_index':index,'source_label':obj['label'],'expected_infected':obj['expected_infected'],'predicted_infected':pred['verdict']=='diseased','predicted_verdict':pred['verdict'],'internal_class_audit_only':pred['class'],'winning_class_score_not_binary_probability':pred['confidence'],'source_box':obj['frame_box'],'crop_bounds':[x,y,x+side,y+side],'crop':'crops/'+name,'crop_sha256':hashlib.sha256(photo.read_bytes()).hexdigest(),'request_ms':pred['request_ms'],'forward_ms':pred['forward_ms'],'model_sha256':pred['model_sha256']}
        rows.append(row)
    print(json.dumps({'event':'annotated_leaves_finished','scene':scene['id'],'total':len(rows)}),flush=True)
report={'scope':'Classifier-only on source-annotated leaf-region crops from the same 720p frames, square +20% context, resized384; bypasses B2 and heuristic; no tuning/retraining; evaluates binary verdict only','model_load_ms':predictor.load_ms,'rows':rows}
(out/'results.json').write_text(json.dumps(report,indent=2))
