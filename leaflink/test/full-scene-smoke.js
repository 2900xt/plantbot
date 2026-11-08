// Actual durable pipeline and production adapters; manifest-driven 1-Hz submissions.
import {readFile,writeFile,mkdir} from 'node:fs/promises';
import path from 'node:path';
import {performance} from 'node:perf_hooks';
import {createPipeline} from '../server/pipeline.js';
import {config} from '../server/config.js';
import {extractLeaves} from '../server/leaves.js';
import {infer} from '../server/model.js';
const root=path.resolve(process.argv[2]),out=path.resolve(process.argv[3]);
await mkdir(out,{recursive:true});
const manifest=JSON.parse(await readFile(path.join(root,'manifest.json'),'utf8'));
const c=config({...process.env,API_TOKEN:'isolated-smoke-test-token-not-production',DATA_DIR:path.join(out,'runtime'),STORAGE_DRIVER:'local',MODEL_MODE:'gpu',LEAF_CROP_MODE:'heuristic',GROUND_MASK_MODE:'local',GPU_MODEL_DIR:path.resolve('models/LeafCNN-Epoch8-GPU'),GROUND_MODEL_DIR:path.resolve('models/PlantLeafPipeline/models/segformer-b2-ade20k'),PYTHON_BIN:process.env.PYTHON_BIN||'python3',MODEL_TIMEOUT_MS:'300000',CROP_TIMEOUT_MS:'600000'});
const records=[];let clock,current=null,producersDone=false;
const p=await createPipeline(c,{
  extract:async(...args)=>{const start=performance.now();try{return await extractLeaves(c,...args);}finally{current.extraction_ms=(current.extraction_ms||0)+performance.now()-start;}},
  infer:async(...args)=>{const start=performance.now();try{return await infer(c,...args);}finally{current.classification_ms=(current.classification_ms||0)+performance.now()-start;current.classifier_calls=(current.classifier_calls||0)+1;}}
});
const sleep=ms=>new Promise(resolve=>setTimeout(resolve,ms));
clock=performance.now();
const epoch=Date.now();
const producer=(async()=>{
  for(const [index,scene] of manifest.scenes.entries()){
    await sleep(Math.max(0,index*1000-(performance.now()-clock)));
    const started=performance.now(),bytes=await readFile(path.join(root,scene.frame));
    const capture=await p.submit(bytes,'smoke-camera',`smoke-frame-${index+1}`,null,{captured_at:new Date(epoch+index*1000).toISOString(),location:{lat:40+index*.00001,lon:-74}});
    const record={scene_id:scene.id,capture_id:capture.id,scheduled_arrival_ms:index*1000,received_ms:started-clock,accepted_ms:performance.now()-clock,submission_ms:performance.now()-started,extraction_ms:0,classification_ms:0,classifier_calls:0};
    records.push(record);console.log(JSON.stringify({event:'accepted',scene:scene.id}));
  }producersDone=true;
})();
try{
  while(!producersDone||records.some(r=>!r.finished)){
    const next=records.find(r=>!r.finished);
    if(!next){await sleep(25);continue;}
    current=next;const started=performance.now();
    if(next.processing_started_ms==null)next.processing_started_ms=started-clock;
    await p.tick();
    const row=p.get(next.capture_id);
    if(['complete','no_candidates','retake_blurry','awaiting_model','awaiting_mask','failed'].includes(row.status)){
      next.finished=true;next.finished_ms=performance.now()-clock;next.processing_ms=next.finished_ms-next.processing_started_ms;
      next.queue_ms=next.processing_started_ms-next.accepted_ms;next.end_to_end_ms=next.finished_ms-next.received_ms;
      next.capture=row;
      console.log(JSON.stringify({event:'finished',scene:next.scene_id,status:row.status,processing_ms:next.processing_ms,candidates:row.leaves.length}));
      await writeFile(path.join(out,'partial-results.json'),JSON.stringify(records,null,2));
    }else await sleep(100);
    if(performance.now()-clock>30*60*1000)throw new Error('Smoke test exceeded 30 minute bound');
  }
  await producer;
  const result={schema:1,scope:'Actual createPipeline submit/tick; real B2 CPU subprocess + supplied v2 + resident CUDA epoch8 FP16; local object storage; excludes camera/network transport; no warmup, first classifier load included in first qualifying scene',interval_ms:1000,frame_size:[1280,720],synthetic_locations:true,blur_threshold:c.blurThreshold,crop_sharpness_threshold:c.cropSharpnessThreshold,threshold:p.field.settings().infected_percent,field:p.field.overview(),events:p.field.events(),records};
  await writeFile(path.join(out,'results.json'),JSON.stringify(result,null,2));
}finally{p.close();}
