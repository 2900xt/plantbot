import {spawn} from 'node:child_process';
import {fileURLToPath} from 'node:url';
export function runPython(c,name,payload,timeout,onProgress) {
  return new Promise((resolve,reject)=>{
    const child=spawn(c.pythonBin,[fileURLToPath(new URL(`../python/${name}`,import.meta.url))],{windowsHide:true,env:{...process.env,...(c.pythonModulePath?{PYTHONPATH:c.pythonModulePath}:{}),HF_HUB_OFFLINE:'1',TRANSFORMERS_OFFLINE:'1'},stdio:['pipe','pipe','pipe']});
    let chunks=[],size=0,errors=[],errorSize=0,finished=false,pending='';
    const finish=(error,value)=>{if(finished)return;finished=true;clearTimeout(timer);error?reject(error):resolve(value);};
    const timer=setTimeout(()=>{child.kill();finish(new Error(`${name} timed out`));},timeout);
    child.on('error',error=>finish(error));child.stdin.on('error',()=>{});
    child.stdout.on('data',chunk=>{size+=chunk.length;if(size>32*1024*1024){child.kill();finish(new Error('Python output too large'));}else chunks.push(chunk);});
    child.stderr.on('data',chunk=>{
      pending+=chunk.toString('utf8');
      const lines=pending.split('\n');pending=lines.pop().slice(-8192);
      for(const line of lines) {
        if(line.startsWith('LEAFLINK_PROGRESS:')) {try{onProgress?.(JSON.parse(line.slice(18)));}catch{}}
        else if(errorSize<4096){errors.push(Buffer.from(line+'\n'));errorSize+=line.length;}
      }
    });
    child.on('close',code=>{if(code!==0)return finish(new Error(`${name}: ${Buffer.concat(errors).toString('utf8').slice(-2000)}`));try{finish(null,JSON.parse(Buffer.concat(chunks).toString('utf8')));}catch{finish(new Error('Invalid Python output'));}});
    child.stdin.end(JSON.stringify(payload));
  });
}
