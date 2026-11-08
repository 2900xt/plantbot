import {spawn} from 'node:child_process';
import {fileURLToPath} from 'node:url';
// One resident CUDA model per pipeline, serialized by the durable queue.
export function gpuPredict(c,bytes) {
  if(!c.gpuWorker) {
    const child=spawn(c.pythonBin,[fileURLToPath(new URL('../python/gpu_worker.py',import.meta.url)),c.gpuModelDir],
      {windowsHide:true,env:{...process.env,...(c.pythonModulePath?{PYTHONPATH:c.pythonModulePath}:{}),HF_HUB_OFFLINE:'1'},stdio:['pipe','pipe','pipe']});
    const worker={child,pending:null,buffer:''};c.gpuWorker=worker;
    const stop=()=>{if(c.gpuWorker===worker)c.gpuWorker=null;if(worker.pending){clearTimeout(worker.pending.timer);worker.pending.reject(Object.assign(new Error('CUDA worker unavailable; check Python dependencies and CUDA GPU'),{code:'MODEL_UNAVAILABLE'}));worker.pending=null;}};
    child.on('error',stop);child.on('exit',stop);child.stdin.on('error',stop);
    child.stderr.on('data',()=>{});
    child.stdout.on('data',chunk=>{
      worker.buffer+=chunk.toString('utf8');
      if(worker.buffer.length>65536){child.kill();stop();return;}
      let index;
      while((index=worker.buffer.indexOf('\n'))>=0){
        const line=worker.buffer.slice(0,index);worker.buffer=worker.buffer.slice(index+1);
        const pending=worker.pending;if(!pending)continue;
        clearTimeout(pending.timer);worker.pending=null;
        try {const value=JSON.parse(line);if(value.error)pending.reject(Object.assign(new Error(value.error),{code:value.code}));else pending.resolve(value);}
        catch{pending.reject(new Error('Invalid CUDA worker response'));}
      }
    });
  }
  const worker=c.gpuWorker;
  return new Promise((resolve,reject)=>{
    if(worker.pending)return reject(new Error('CUDA worker is busy'));
    const timer=setTimeout(()=>{worker.pending=null;worker.child.kill();if(c.gpuWorker===worker)c.gpuWorker=null;reject(new Error('CUDA inference timed out'));},Math.max(c.modelTimeout,120000));
    worker.pending={resolve,reject,timer};worker.child.stdin.write(JSON.stringify({image:bytes.toString('base64')})+'\n');
  });
}
