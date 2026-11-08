import {spawn} from 'node:child_process';
import {fileURLToPath} from 'node:url';

const unavailable=()=>Object.assign(new Error('Checkpoint 1 segmentation model unavailable; run model setup and check the Python runtime'),{code:'MODEL_UNAVAILABLE'});

export function closeSegmentedWorker(c) {c.segmentedWorker?.stop(unavailable());}

export function segmentedPredict(c,bytes,context) {
  if (!c.segmentedWorker) {
    const child=spawn(c.pythonBin,[fileURLToPath(new URL('../python/segmented_leaf_worker.py',import.meta.url)),c.segmentedModelDir,c.segmentationModelDir,c.segmentedDevice,String(c.segmentationCpuThreads)],{
      windowsHide:true,env:{...process.env,...(c.pythonModulePath?{PYTHONPATH:c.pythonModulePath}:{}),HF_HUB_OFFLINE:'1',TRANSFORMERS_OFFLINE:'1'},stdio:['pipe','pipe','pipe']});
    const worker={child,pending:null,buffer:''};c.segmentedWorker=worker;
    worker.stop=error=>{if(c.segmentedWorker===worker)c.segmentedWorker=null;if(worker.pending){clearTimeout(worker.pending.timer);worker.pending.reject(error);worker.pending=null;}child.kill();};
    child.on('error',()=>worker.stop(unavailable()));child.on('exit',()=>worker.stop(unavailable()));child.stdin.on('error',()=>worker.stop(unavailable()));
    child.stderr.on('data',()=>{});
    child.stdout.on('data',chunk=>{
      worker.buffer+=chunk.toString('utf8');
      if(worker.buffer.length>65536){worker.stop(unavailable());return;}
      let index;
      while((index=worker.buffer.indexOf('\n'))>=0){
        const line=worker.buffer.slice(0,index);worker.buffer=worker.buffer.slice(index+1);const pending=worker.pending;if(!pending)continue;
        clearTimeout(pending.timer);worker.pending=null;
        try{const value=JSON.parse(line);if(value.error){pending.reject(unavailable());worker.stop(unavailable());}else pending.resolve(value);}
        catch{pending.reject(unavailable());worker.stop(unavailable());}
      }
    });
  }
  const worker=c.segmentedWorker;
  return new Promise((resolve,reject)=>{
    if(worker.pending){reject(new Error('Segmented classifier is busy'));return;}
    const timer=setTimeout(()=>worker.stop(unavailable()),Math.max(c.modelTimeout,180000));
    worker.pending={resolve,reject,timer};worker.child.stdin.write(JSON.stringify({image:bytes.toString('base64'),context})+'\n');
  });
}
