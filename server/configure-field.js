import {existsSync,readFileSync,writeFileSync} from 'node:fs';
import {randomBytes} from 'node:crypto';
import path from 'node:path';
import {migrateDefaultClassifierEnv} from '../scripts/setup-lib.mjs';
const experts=process.argv.includes('--experts');
const envFile=experts?'.env.experts':'.env.field';
if(!existsSync(envFile)){
  const managed=process.platform==='win32'?'.venv-field/Scripts/python.exe':'.venv-field/bin/python';
  const pythonPath=existsSync(managed)?path.resolve(managed):process.platform==='win32'?'py':'python3';
  const modules=existsSync(managed)?'':existsSync('../.test-deps/torch')?path.resolve('../.test-deps'):'';
  const env=`HOST=127.0.0.1\nPORT=8080\nAPI_TOKEN=${randomBytes(32).toString('hex')}\nDATA_DIR=./${experts?'runtime-experts':'runtime-field'}\nSTORAGE_DRIVER=local\nMODEL_MODE=${experts?'experts':'segmented'}\nSEGMENTED_MODEL_DIR=./models/LeafCNN-Field-Checkpoint1\nSEGMENTATION_MODEL_DIR=./models/SAM2.1-Hiera-Tiny\nSEGMENTED_DEVICE=auto\nSEGMENTATION_CPU_THREADS=4\nEXPERT_MODEL_DIR=./models/CropExperts-ResNet18\nEXPERT_DEVICE=auto\nGPU_MODEL_DIR=./models/LeafCNN-Epoch8-GPU\nGROUND_MODEL_DIR=./models/PlantLeafPipeline/models/segformer-b2-ade20k\nLEAF_CROP_MODE=heuristic\nLEAF_HEURISTIC_VERSION=v2\nMAX_LEAF_CANDIDATES=40\nPYTHON_BIN=${pythonPath.replaceAll('\\','/')}\nPYTHON_MODULE_PATH=${modules.replaceAll('\\','/')}\nMODEL_TIMEOUT_MS=120000\nCROP_TIMEOUT_MS=180000\n`;
  writeFileSync(envFile,env+'LEAF_BLUR_THRESHOLD=0.00025\nMIN_CROP_SHARPNESS=0.0001\n',{flag:'wx'});
}
if(!experts && existsSync(envFile)){
  const previous=readFileSync(envFile,'utf8'),updated=migrateDefaultClassifierEnv(previous,process.cwd());
  if(updated!==previous){writeFileSync(envFile,updated);console.log('Updated the packaged default classifier to replay-protected checkpoint 1 with SAM2.');}
}
if(experts) console.log('Experimental crop experts selected. Field smoke accuracy regressed; the segmented classifier remains the normal default.');
if(process.argv.includes('--show-token')){
  const token=readFileSync(envFile,'utf8').match(/^API_TOKEN=(.+)$/m)?.[1];
  console.log(`Open http://localhost:8080 and paste this API token into Connection:\n${token}\nKeep this window open while using LeafLink.`);
}
