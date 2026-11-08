// Built-in Node modules only: works before npm ci, with no global Python required.
import {existsSync} from 'node:fs';
import {mkdir,readFile,writeFile,rm} from 'node:fs/promises';
import path from 'node:path';
import {fileURLToPath} from 'node:url';
import {gpuPolicy,run,sha,downloadChecked,npmCli,setRuntimeFile} from './scripts/setup-lib.mjs';

const root=path.dirname(fileURLToPath(import.meta.url));
const stateDir=path.join(root,'.leaflink'),tools=path.join(stateDir,'tools');
const args=new Set(process.argv.slice(2));
const supported=new Set(['--start','--check','--experts','--setup-only']);
const experts=args.has('--experts'),check=args.has('--check');
const envFile=path.join(root,experts?'.env.experts':'.env.field');
const python=path.join(root,'.venv-field',process.platform==='win32'?'Scripts/python.exe':'bin/python');
const env={...process.env,PYTHONNOUSERSITE:'1',PYTHON_MODULE_PATH:'',UV_PYTHON_INSTALL_DIR:path.join(stateDir,'python'),UV_CACHE_DIR:path.join(stateDir,'cache/uv'),UV_PYTHON_INSTALL_BIN:'0'};
delete env.PYTHONPATH;
let locked=false;
const lock=path.join(stateDir,'setup.lock');
async function acquire(){
  await mkdir(stateDir,{recursive:true});
  if(existsSync(lock)){
    let pid;try{pid=JSON.parse(await readFile(lock,'utf8')).pid;}catch{}
    if(Number.isInteger(pid)&&pid>0){try{process.kill(pid,0);throw new Error('Another LeafLink setup is running. Wait for its window to finish.');}catch(error){if(error.code!=='ESRCH')throw error;}}
    await rm(lock,{force:true});
  }
  await writeFile(lock,JSON.stringify({pid:process.pid}),{flag:'wx'});locked=true;
}
async function uvTool(){
  const windows=process.platform==='win32';
  const name=windows?'uv-x86_64-pc-windows-msvc.zip':'uv-x86_64-unknown-linux-gnu.tar.gz';
  const digest=windows?'0cdc5b38fe5a8168ed6a25814f5ba43c21d313dd3187314fccb37b7cec46e021':'c5221ff41b7892bfc223543883600125f6a47fe845093e0f5bdff4f359196d83';
  const folder=path.join(tools,'uv-0.13.7'),archive=path.join(tools,name);
  const executable=path.join(folder,windows?'uv.exe':'uv-x86_64-unknown-linux-gnu/uv');
  if(!existsSync(executable)){
    console.log('Installing the project-local Python setup tool…');
    await downloadChecked('https://github.com/astral-sh/uv/releases/download/0.13.7/'+name,archive,digest);
    await mkdir(folder,{recursive:true});
    if(windows){
      // Argument array, no interpolated shell command or executable download script.
      const script=path.join(root,'scripts/expand-tool.ps1');
      await run('powershell.exe',['-NoProfile','-ExecutionPolicy','Bypass','-File',script,'-Archive',archive,'-Destination',folder],{cwd:root,env});
    }else await run('tar',['-xf',archive,'-C',folder],{cwd:root,env});
  }
  const version=await run(executable,['--version'],{cwd:root,env,capture:true});
  if(!/^uv 0\.13\.7(?:\s|$)/.test(version))throw new Error('Unexpected local uv version. Move .leaflink/tools/uv-0.13.7 aside and run setup again.');
  return executable;
}
try{
  for(const arg of args)if(!supported.has(arg))throw new Error('Unknown setup option: '+arg);
  if(!['win32','linux'].includes(process.platform)||process.arch!=='x64')throw new Error('The automatic installer supports x64 Windows 10/11 and modern x64 Linux with an NVIDIA GPU.');
  if(Number(process.versions.node.split('.')[0])<24)throw new Error('Node 24+ is required. Use Start-LeafLink.cmd or start-leaflink.sh.');
  console.log('Checking NVIDIA hardware and driver…');
  let smi='nvidia-smi';
  if(process.platform==='win32'){
    for(const candidate of [path.join(process.env.SystemRoot||'C:/Windows','System32/nvidia-smi.exe'),path.join(process.env.ProgramFiles||'C:/Program Files','NVIDIA Corporation/NVSMI/nvidia-smi.exe')])if(existsSync(candidate)){smi=candidate;break;}
  }
  let policy;
  try{policy=gpuPolicy(await run(smi,['--query-gpu=name,driver_version,compute_cap','--format=csv,noheader,nounits'],{cwd:root,env,capture:true}));}
  catch(error){throw new Error(error.message+'\nAn NVIDIA GPU alone is insufficient: its driver must be installed and working. https://www.nvidia.com/Download/index.aspx');}
  console.log(`${policy.name}; driver ${policy.driver}; CUDA wheel ${policy.wheel}.`);
  if(check){
    if(!existsSync(python))throw new Error('The managed environment is not installed. Run the normal launcher first.');
    await run(process.execPath,['setup-models.js','--check'],{cwd:root,env});
    await run(python,['scripts/check-runtime.py','--full',...(experts?['--experts']:[])],{cwd:root,env});
    console.log('LeafLink runtime and model checks passed.');
  }else{
    await acquire();
    const uv=await uvTool();
    if(!existsSync(python)){
      console.log('Creating an isolated Python 3.12 environment…');
      await run(uv,['venv','--managed-python','--python','3.12',path.join(root,'.venv-field')],{cwd:root,env});
    }
    const stateFile=path.join(stateDir,'setup-state.json');let previous={};
    try{previous=JSON.parse(await readFile(stateFile,'utf8'));}catch{}
    const runtimeKey=JSON.stringify({python,requirements:await sha(path.join(root,'requirements-runtime.txt')),torch:'2.11.0',vision:'0.26.0',wheel:policy.wheel});
    if(previous.runtimeKey!==runtimeKey){
      console.log('Installing CUDA PyTorch and the image-processing dependencies (first launch may take several minutes)…');
      await run(uv,['pip','install','--python',python,'--index-url','https://download.pytorch.org/whl/'+policy.wheel,'torch==2.11.0+'+policy.wheel,'torchvision==0.26.0+'+policy.wheel],{cwd:root,env});
      await run(uv,['pip','install','--python',python,'--index-url','https://pypi.org/simple','-r',path.join(root,'requirements-runtime.txt')],{cwd:root,env});
    }
    await run(python,['scripts/check-runtime.py','--cuda-only'],{cwd:root,env});
    console.log('Downloading/verifying the published model weights…');
    await run(process.execPath,['setup-models.js'],{cwd:root,env});
    const npm=await npmCli(),lockKey=await sha(path.join(root,'package-lock.json'));
    if(previous.lockKey!==lockKey||!existsSync(path.join(root,'node_modules/vite/bin/vite.js'))||!existsSync(path.join(root,'node_modules/express/package.json'))){
      console.log('Installing the locked web-app dependencies…');
      await run(process.execPath,[npm,'ci','--include=dev','--no-audit','--no-fund'],{cwd:root,env});
    }
    console.log('Building the web app…');
    await run(process.execPath,[npm,'run','build'],{cwd:root,env});
    console.log('Checking real model loading and a synthetic GPU inference…');
    await run(python,['scripts/check-runtime.py','--full',...(experts?['--experts']:[])],{cwd:root,env});
    await run(process.execPath,['server/configure-field.js',...(experts?['--experts']:[])],{cwd:root,env});
    await setRuntimeFile(envFile,python);
    await writeFile(stateFile,JSON.stringify({runtimeKey,lockKey,checked_at:new Date().toISOString(),gpu:policy.name,wheel:policy.wheel},null,2));
    console.log('Setup passed. Models, CUDA runtime and compiled frontend are ready.');
  }
}catch(error){
  console.error('\nLeafLink setup stopped: '+error.message);
  console.error('Nothing was started. Fix the reported issue and run the same launcher again. See README.md.');
  process.exitCode=1;
}finally{if(locked)await rm(lock,{force:true});}
if(!process.exitCode&&args.has('--start')&&!args.has('--setup-only')&&!check){
  try{
    await run(process.execPath,['server/configure-field.js',...(experts?['--experts']:[]),'--show-token'],{cwd:root,env});
    await run(process.execPath,['--env-file='+envFile,'server/index.js'],{cwd:root,env});
  }catch(error){console.error('LeafLink could not start: '+error.message);process.exitCode=1;}
}
