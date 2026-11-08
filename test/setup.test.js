import test from 'node:test';
import assert from 'node:assert/strict';
import {mkdtemp,writeFile,readFile,rm} from 'node:fs/promises';
import os from 'node:os';
import path from 'node:path';
import {parseEnv} from 'node:util';
import {gpuPolicy,updateRuntimeEnv,run,setRuntimeFile} from '../scripts/setup-lib.mjs';

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
