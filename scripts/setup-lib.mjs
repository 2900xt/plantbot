import {spawn} from 'node:child_process';
import {existsSync} from 'node:fs';
import {readFile,writeFile,mkdir,rename,rm} from 'node:fs/promises';
import {createHash,randomUUID} from 'node:crypto';
import {createWriteStream} from 'node:fs';
import {Readable,Transform} from 'node:stream';
import {pipeline} from 'node:stream/promises';
import path from 'node:path';
import {parseEnv} from 'node:util';

export function migrateDefaultClassifierEnv(text,root){
  const current=parseEnv(text);
  if(current.MODEL_MODE!=='gpu')return text;
  const canonical=value=>{const resolved=path.resolve(root,value.replaceAll('\\','/'));return process.platform==='win32'?resolved.toLowerCase():resolved;};
  if(current.GPU_MODEL_DIR && canonical(current.GPU_MODEL_DIR)!==canonical('./models/LeafCNN-Epoch8-GPU'))return text;
  let updated=text.replace(/^[ \t]*MODEL_MODE[ \t]*=.*$/gm,'MODEL_MODE=segmented');
  const defaults={SEGMENTED_MODEL_DIR:'./models/LeafCNN-Field-Checkpoint1',SEGMENTATION_MODEL_DIR:'./models/SAM2.1-Hiera-Tiny',SEGMENTED_DEVICE:'auto',SEGMENTATION_CPU_THREADS:'4'};
  for(const [key,value] of Object.entries(defaults))if(current[key]===undefined)updated=updated.trimEnd()+'\n'+key+'='+value+'\n';
  return updated;
}

export function gpuPolicy(text){
  const rows=text.trim().split(/\r?\n/).filter(Boolean).map(line=>{
    const parts=line.split(',').map(value=>value.trim());
    if(parts.length<3)throw new Error('Cannot read NVIDIA GPU details. Update the NVIDIA driver and retry.');
    const capability=Number(parts.pop()),driver=parts.pop(),name=parts.join(',');
    return {name,driver,capability};
  });
  if(!rows.length)throw new Error('No NVIDIA GPU was found. Install an NVIDIA driver and retry.');
  // Match CUDA's default GPU; do not silently change the user's device selection.
  const first=rows[0],major=Number(first.driver.split('.')[0]);
  if(!Number.isFinite(first.capability)||first.capability<7.5)throw new Error(`${first.name}: this installer supports Turing or newer GPUs (compute capability 7.5+). Older GPUs need a separately validated runtime.`);
  if(!Number.isFinite(major)||major<570)throw new Error(`NVIDIA driver ${first.driver} is too old for the supported runtime. Update it at https://www.nvidia.com/Download/index.aspx and restart Windows/Linux before retrying.`);
  return {...first,wheel:major>=580?'cu130':'cu128'};
}

export function updateRuntimeEnv(text,python){
  // Preserve tokens, storage, thresholds and model selection. Own only runtime paths.
  const kept=text.split(/\r?\n/).filter(line=>!/^\s*(?:PYTHON_BIN|PYTHON_MODULE_PATH)\s*=/.test(line));
  while(kept.at(-1)==='')kept.pop();
  return kept.join('\n')+'\nPYTHON_BIN='+JSON.stringify(python.replaceAll('\\','/'))+'\nPYTHON_MODULE_PATH=\n';
}

export function run(command,args,{cwd,env=process.env,capture=false}={}){
  return new Promise((resolve,reject)=>{
    const child=spawn(command,args,{cwd,env,windowsHide:true,stdio:capture?['ignore','pipe','pipe']:'inherit'});
    let output='',errors='';
    if(capture){child.stdout.on('data',chunk=>{output+=chunk;if(output.length>2e6){child.kill();reject(new Error('Setup command produced too much output'));}});child.stderr.on('data',chunk=>{errors=(errors+chunk).slice(-8000);});}
    child.on('error',error=>reject(new Error(`Could not start ${path.basename(command)}: ${error.message}`)));
    child.on('close',code=>code===0?resolve(output.trim()):reject(new Error(`${path.basename(command)} failed (${code}). ${capture?errors:''}`.trim())));
  });
}

export async function sha(file){return createHash('sha256').update(await readFile(file)).digest('hex');}

export async function downloadChecked(url,file,expected){
  if(existsSync(file)&&await sha(file)===expected)return;
  await mkdir(path.dirname(file),{recursive:true});
  const temp=file+'.download-'+randomUUID();
  try{
    const response=await fetch(url,{signal:AbortSignal.timeout(15*60*1000)});
    if(!response.ok||!response.body)throw new Error(`Download failed (${response.status}): ${url}`);
    const hash=createHash('sha256');
    await pipeline(Readable.fromWeb(response.body),new Transform({transform(chunk,encoding,callback){hash.update(chunk);callback(null,chunk);}}),createWriteStream(temp,{flags:'wx'}));
    if(hash.digest('hex')!==expected)throw new Error('Portable tool checksum mismatch; nothing was installed. Retry from the official repository.');
    if(existsSync(file))await rm(file);
    await rename(temp,file);
  }finally{await rm(temp,{force:true});}
}

export async function npmCli(){
  const directory=path.dirname(process.execPath);
  const options=[process.env.npm_execpath,path.join(directory,'node_modules/npm/bin/npm-cli.js'),path.resolve(directory,'../lib/node_modules/npm/bin/npm-cli.js')];
  for(const entry of (process.env.PATH||'').split(path.delimiter))options.push(path.join(entry,'node_modules/npm/bin/npm-cli.js'));
  for(const file of options)if(file&&existsSync(file)&&path.basename(file)==='npm-cli.js')return file;
  // Debian's system Node package places npm here; portable Node takes the paths above.
  const debian='/usr/share/nodejs/npm/bin/npm-cli.js';if(existsSync(debian))return debian;
  throw new Error('npm is missing. Use Start-LeafLink.cmd or start-leaflink.sh to install the bundled Node runtime.');
}

export async function setRuntimeFile(file,python){
  const text=await readFile(file,'utf8');
  const updated=updateRuntimeEnv(text,python);
  if(updated!==text)await writeFile(file,updated,{mode:0o600});
}
