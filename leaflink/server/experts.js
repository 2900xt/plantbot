import {spawn} from 'node:child_process';
import {fileURLToPath} from 'node:url';

const unavailable = () => Object.assign(new Error('Crop experts unavailable. Install the trained expert package and matching PyTorch/torchvision dependencies; check EXPERT_MODEL_DIR and EXPERT_DEVICE.'),{code:'MODEL_UNAVAILABLE'});

export function closeExpertWorker(c) {
  c.expertWorker?.stop(unavailable());
}

// A single resident routed predictor; no fallback to the original classifier.
export function expertPredict(c,bytes) {
  if (!c.expertWorker) {
    const child=spawn(c.pythonBin,[fileURLToPath(new URL('../python/expert_worker.py',import.meta.url)),c.expertModelDir,c.expertDevice,String(c.inferenceThreads)],{
      windowsHide:true,env:{...process.env,...(c.pythonModulePath?{PYTHONPATH:c.pythonModulePath}:{}),HF_HUB_OFFLINE:'1'},stdio:['pipe','pipe','pipe']});
    const worker={child,pending:null,buffer:''}; c.expertWorker=worker;
    worker.stop=error=>{
      if (c.expertWorker===worker) c.expertWorker=null;
      if (worker.pending) {clearTimeout(worker.pending.timer);worker.pending.reject(error);worker.pending=null;}
      child.kill();
    };
    child.on('error',()=>worker.stop(unavailable()));
    child.on('exit',()=>worker.stop(unavailable()));
    child.stdin.on('error',()=>worker.stop(unavailable()));
    child.stderr.on('data',()=>{});
    child.stdout.on('data',chunk=>{
      worker.buffer+=chunk.toString('utf8');
      if (worker.buffer.length>65536) {worker.stop(unavailable());return;}
      let index;
      while ((index=worker.buffer.indexOf('\n'))>=0) {
        const line=worker.buffer.slice(0,index);worker.buffer=worker.buffer.slice(index+1);
        const pending=worker.pending;
        if (!pending) continue;
        clearTimeout(pending.timer);worker.pending=null;
        try {
          const result=JSON.parse(line);
          if (result.error) {pending.reject(unavailable());worker.stop(unavailable());}
          else pending.resolve(result);
        } catch {pending.reject(unavailable());worker.stop(unavailable());}
      }
    });
  }
  const worker=c.expertWorker;
  return new Promise((resolve,reject)=>{
    if (worker.pending) {reject(new Error('Crop expert worker is busy'));return;}
    const timer=setTimeout(()=>worker.stop(unavailable()),c.modelTimeout);
    worker.pending={resolve,reject,timer};
    worker.child.stdin.write(JSON.stringify({image:bytes.toString('base64')})+'\n');
  });
}
