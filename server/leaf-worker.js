import {spawn} from 'node:child_process';
import {fileURLToPath} from 'node:url';

export function closeLeafWorker(c) {
  const worker=c.leafWorker;
  if (!worker) return;
  worker.fail(new Error('Leaf extraction worker closed'));
}

// The durable pipeline runs one frame at a time. A failed/timed-out process is
// discarded so its late reply can never be delivered to the following frame.
export function residentExtract(c,payload,onProgress) {
  if (!c.leafWorker) {
    const child=spawn(c.pythonBin,[fileURLToPath(new URL('../python/leaf_worker.py',import.meta.url))],{
      windowsHide:true,env:{...process.env,...(c.pythonModulePath?{PYTHONPATH:c.pythonModulePath}:{}),
        HF_HUB_OFFLINE:'1',TRANSFORMERS_OFFLINE:'1'},stdio:['pipe','pipe','pipe']});
    const worker={child,pending:null,buffer:'',errors:''};
    c.leafWorker=worker;
    worker.fail=error=>{
      if (c.leafWorker===worker) c.leafWorker=null;
      if (worker.pending) {clearTimeout(worker.pending.timer);worker.pending.reject(error);worker.pending=null;}
      child.kill();
    };
    child.on('error',()=>worker.fail(new Error('Leaf extraction worker could not start')));
    child.on('exit',()=>worker.fail(new Error('Leaf extraction worker exited')));
    child.stdin.on('error',()=>worker.fail(new Error('Leaf extraction worker disconnected')));
    child.stderr.on('data',chunk=>{
      worker.errors+=chunk.toString('utf8');
      let index;
      while ((index=worker.errors.indexOf('\n'))>=0) {
        const line=worker.errors.slice(0,index);worker.errors=worker.errors.slice(index+1);
        if (line.startsWith('LEAFLINK_PROGRESS:')) {
          try {worker.pending?.onProgress?.(JSON.parse(line.slice(18)));} catch {}
        }
      }
      worker.errors=worker.errors.slice(-8192);
    });
    child.stdout.on('data',chunk=>{
      worker.buffer+=chunk.toString('utf8');
      if (worker.buffer.length>32*1024*1024) return worker.fail(new Error('Leaf extraction output too large'));
      let index;
      while ((index=worker.buffer.indexOf('\n'))>=0) {
        const line=worker.buffer.slice(0,index);worker.buffer=worker.buffer.slice(index+1);
        const pending=worker.pending;
        if (!pending) continue;
        clearTimeout(pending.timer);worker.pending=null;
        try {
          const value=JSON.parse(line);
          if (value.error) pending.reject(new Error(value.error));
          else pending.resolve(value);
        } catch {worker.fail(new Error('Invalid extraction worker response'));pending.reject(new Error('Invalid extraction worker response'));}
      }
    });
  }
  const worker=c.leafWorker;
  return new Promise((resolve,reject)=>{
    if (worker.pending) return reject(new Error('Leaf extraction worker is busy'));
    const timer=setTimeout(()=>worker.fail(new Error('Leaf extraction timed out')),c.cropTimeout);
    worker.pending={resolve,reject,timer,onProgress};
    worker.child.stdin.write(JSON.stringify(payload)+'\n');
  });
}
