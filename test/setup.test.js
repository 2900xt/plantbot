import test from 'node:test';
import assert from 'node:assert/strict';
import {mkdtemp,writeFile,readFile,rm} from 'node:fs/promises';
import os from 'node:os';
import path from 'node:path';
import {parseEnv} from 'node:util';
import {createHash} from 'node:crypto';
import {fileURLToPath} from 'node:url';
import {gpuPolicy,updateRuntimeEnv,run,setRuntimeFile,migrateDefaultClassifierEnv} from '../scripts/setup-lib.mjs';

test('only the old packaged GPU default migrates; user choices and credentials survive',()=>{
  const root=path.resolve('/leaflink');
  const source='API_TOKEN=keep-secret\nPORT=9000\nMODEL_MODE=gpu\nGPU_MODEL_DIR=./models/LeafCNN-Epoch8-GPU\nBINARY_DISEASE_THRESHOLD=.9\n';
  const updated=migrateDefaultClassifierEnv(source,root),parsed=parseEnv(updated);
  assert.equal(parsed.MODEL_MODE,'segmented');assert.equal(parsed.API_TOKEN,'keep-secret');assert.equal(parsed.PORT,'9000');assert.equal(parsed.BINARY_DISEASE_THRESHOLD,'.9');
  assert.equal(parsed.SEGMENTED_MODEL_DIR,'./models/LeafCNN-Field-Checkpoint1');assert.equal(migrateDefaultClassifierEnv(updated,root),updated);
  for(const mode of ['http','local','experts','disabled','segmented']){
    const custom=source.replace('MODEL_MODE=gpu','MODEL_MODE='+mode);assert.equal(migrateDefaultClassifierEnv(custom,root),custom);
  }
  const external=source.replace('./models/LeafCNN-Epoch8-GPU','./custom/checkpoint');assert.equal(migrateDefaultClassifierEnv(external,root),external);
  const customDevice=source+'SEGMENTED_DEVICE=cpu\n';assert.equal(parseEnv(migrateDefaultClassifierEnv(customDevice,root)).SEGMENTED_DEVICE,'cpu');
});

test('fresh launcher selects segmented mode and leaves custom model configuration intact',async t=>{
  const directory=await mkdtemp(path.join(os.tmpdir(),'leaflink-segmented-setup-'));t.after(()=>rm(directory,{recursive:true,force:true}));
  const script=fileURLToPath(new URL('../server/configure-field.js',import.meta.url));
  await run(process.execPath,[script],{cwd:directory,capture:true});
  const file=path.join(directory,'.env.field'),initial=await readFile(file,'utf8'),config=parseEnv(initial);
  assert.equal(config.MODEL_MODE,'segmented');assert.equal(config.SEGMENTED_DEVICE,'auto');
  assert.equal(config.SEGMENTATION_MODEL_DIR,'./models/SAM2.1-Hiera-Tiny');
  const custom=initial.replace('MODEL_MODE=segmented','MODEL_MODE=http')+'MODEL_URL=https://example.invalid/model\n';
  await writeFile(file,custom);await run(process.execPath,[script],{cwd:directory,capture:true});
  assert.equal(await readFile(file,'utf8'),custom);
});

test('selects a matching supported CUDA wheel and rejects old drivers/architectures',()=>{
  assert.equal(gpuPolicy('Tesla T4, 590.48.01, 7.5').wheel,'cu130');
  assert.equal(gpuPolicy('NVIDIA RTX 3080, 570.86, 8.6').wheel,'cu128');
  assert.throws(()=>gpuPolicy('RTX 3080, 550.01, 8.6'),/too old/);
  assert.throws(()=>gpuPolicy('GTX 1080, 580.01, 6.1'),/Turing/);
  assert.throws(()=>gpuPolicy(''),/No NVIDIA/);
  assert.throws(()=>gpuPolicy('malformed'),/Cannot read/);
});
test('runtime configuration preserves secrets and settings, supports spaces/hash and is idempotent',()=>{
  const source='# user settings\r\nAPI_TOKEN=keep-this-value\r\nMODEL_MODE=experts\r\nPORT=9000\r\nPYTHON_BIN=old\r\nPYTHON_MODULE_PATH=old-modules\r\nPYTHON_BIN=duplicate\r\n';
  const python='C:\\Plant #1\\.venv-field\\Scripts\\python.exe';
  const updated=updateRuntimeEnv(source,python),parsed=parseEnv(updated);
  assert.equal(parsed.API_TOKEN,'keep-this-value');assert.equal(parsed.PORT,'9000');assert.equal(parsed.MODEL_MODE,'experts');
  assert.equal(parsed.PYTHON_BIN,'C:/Plant #1/.venv-field/Scripts/python.exe');assert.equal(parsed.PYTHON_MODULE_PATH,'');
  assert.equal((updated.match(/^PYTHON_BIN=/gm)||[]).length,1);assert.equal(updateRuntimeEnv(updated,python),updated);
});
test('subprocess execution preserves path arguments and reports failures',async()=>{
  assert.equal(await run(process.execPath,['-e','process.stdout.write(process.argv[1])','a path with spaces # and $'],{capture:true}),'a path with spaces # and $');
  await assert.rejects(()=>run(process.execPath,['-e','process.stderr.write("probe failure");process.exit(9)'],{capture:true}),/probe failure/);
});
test('environment update writes only the managed interpreter keys',async t=>{
  const directory=await mkdtemp(path.join(os.tmpdir(),'leaflink-env-'));t.after(()=>rm(directory,{recursive:true,force:true}));
  const file=path.join(directory,'.env.field');await writeFile(file,'API_TOKEN=keep\nGROUND_DEVICE=cuda\n');
  await setRuntimeFile(file,'/project/.venv-field/bin/python');
  const result=parseEnv(await readFile(file,'utf8'));assert.equal(result.API_TOKEN,'keep');assert.equal(result.GROUND_DEVICE,'cuda');assert.equal(result.PYTHON_BIN,'/project/.venv-field/bin/python');
});
test('model check-only verifies local hashes and never downloads missing weights',async t=>{
  const directory=await mkdtemp(path.join(os.tmpdir(),'leaflink-model-check-'));t.after(()=>rm(directory,{recursive:true,force:true}));
  const script=path.join(directory,'setup-models.mjs');await writeFile(script,await readFile(fileURLToPath(new URL('../setup-models.js',import.meta.url))));
  const bytes=Buffer.from('verified model fixture'),digest=createHash('sha256').update(bytes).digest('hex');
  await writeFile(path.join(directory,'model-assets.json'),JSON.stringify({schema:1,assets:[{path:'models/probe.pt',url:'https://github.com/NarwhaLIX/LeafLink/releases/download/probe/probe.pt',sha256:digest,bytes:bytes.length}]}));
  await assert.rejects(()=>run(process.execPath,[script,'--check'],{capture:true}),/Missing model/);
  const {mkdir}=await import('node:fs/promises');await mkdir(path.join(directory,'models'));await writeFile(path.join(directory,'models/probe.pt'),bytes);
  assert.match(await run(process.execPath,[script,'--check'],{capture:true}),/verified/i);
  await writeFile(path.join(directory,'models/probe.pt'),'corrupt');
  await assert.rejects(()=>run(process.execPath,[script,'--check'],{capture:true}),/differs from release/);
});
