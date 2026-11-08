import {runPython} from './python.js';
import {gpuPredict} from './gpu.js';
import {expertPredict} from './experts.js';
import {applyBinaryTriage, probabilityVector} from './binary.js';
export function validateVerdict(value) {
  if (!value || !['diseased', 'healthy'].includes(value.verdict) || typeof value.confidence !== 'number' || !Number.isFinite(value.confidence) || value.confidence < 0 || value.confidence > 1 || typeof value.model_version !== 'string' || !value.model_version.trim() || value.model_version.length > 200) throw new Error('Invalid model response');
  const result = { verdict: value.verdict, confidence: value.confidence, model_version: value.model_version, simulated: false };
  if (typeof value.class_name === 'string' && value.class_name.length <= 200) result.class_name=value.class_name;
  if (Number.isInteger(value.class_index) && value.class_index >= 0 && value.class_index < 38) result.class_index=value.class_index;
  if (Array.isArray(value.input_size) && value.input_size.length===2 && value.input_size.every(size=>Number.isInteger(size)&&size>0&&size<=1024)) result.input_size=value.input_size;
  if (value.score_type==='winning_class_softmax') result.score_type=value.score_type;
  if (Array.isArray(value.top_predictions) && value.top_predictions.length<=3 && value.top_predictions.every(p=>typeof p.class_name==='string'&&p.class_name.length<=200&&typeof p.confidence==='number'&&Number.isFinite(p.confidence)&&p.confidence>=0&&p.confidence<=1)) result.top_predictions=value.top_predictions;
  if (value.classes !== undefined || value.probabilities !== undefined) {
    probabilityVector(value.classes,value.probabilities);
    result.classes=value.classes; result.probabilities=value.probabilities;
  }
  return result;
}
export function validateExpertVerdict(value) {
  const result=validateVerdict(value);
  const probability=p=>typeof p==='number' && Number.isFinite(p) && p>=0 && p<=1;
  if (value.score_type!=='selected_expert_sigmoid' || value.binary_score_type!=='selected_expert_sigmoid' || !probability(value.disease_score) || !probability(value.router_score) || !Number.isInteger(value.expert_index) || value.expert_index<0 || value.expert_index>3 || !Array.isArray(value.expert_crops) || !value.expert_crops.length || value.expert_crops.length>38 || !value.expert_crops.every(crop=>typeof crop==='string' && crop.length>0 && crop.length<=80) || !['cpu','cuda','cuda:0'].includes(value.device) || !Array.isArray(value.input_size) || value.input_size.length!==2 || value.input_size[0]!==224 || value.input_size[1]!==224 || !value.model_version.startsWith('crop-experts-') || typeof value.model_sha256!=='string' || !/^[a-f0-9]{64}$/.test(value.model_sha256) || !Array.isArray(value.router_probabilities) || value.router_probabilities.length!==4 || !value.router_probabilities.every(probability) || Math.abs(value.router_probabilities.reduce((sum,p)=>sum+p,0)-1)>.001 || Math.abs(value.router_probabilities[value.expert_index]-value.router_score)>.00001 || value.router_score<Math.max(...value.router_probabilities) || value.verdict!==(value.disease_score>=.5?'diseased':'healthy')) throw new Error('Invalid crop expert model response');
  Object.assign(result,{score_type:value.score_type,binary_score_type:value.binary_score_type,disease_score:value.disease_score,
    expert_index:value.expert_index,expert_crops:value.expert_crops,router_score:value.router_score,router_probabilities:value.router_probabilities,device:value.device,model_sha256:value.model_sha256});
  if (value.disease_threshold!==undefined) {
    if (!probability(value.disease_threshold)) throw new Error('Invalid crop expert threshold');
    result.disease_threshold=value.disease_threshold;
  }
  return result;
}
export async function infer(c, bytes, id) {
  if (c.modelMode === 'experts') return validateExpertVerdict(await expertPredict(c,bytes));
  if (c.modelMode === 'gpu') return applyBinaryTriage(c,validateVerdict(await gpuPredict(c,bytes)));
  if (c.modelMode === 'demo') return { verdict: c.demoVerdict, confidence: 0.99, model_version: 'demo-no-ml', simulated: true };
  if (c.modelMode === 'local') return applyBinaryTriage(c,validateVerdict(await runPython(c,'predict_leaf.py',{image:bytes.toString('base64'),model_dir:c.localModelDir,cpu_threads:c.inferenceThreads,memory_format:c.inferenceFormat},c.modelTimeout)));
  const body = new FormData();
  body.append('image', new Blob([bytes], { type: 'image/jpeg' }), `${id}.jpg`);
  const response = await fetch(c.modelUrl, { method: 'POST', body, signal: AbortSignal.timeout(c.modelTimeout),
    headers: { 'X-Capture-ID': id, ...(c.modelToken ? { Authorization: `Bearer ${c.modelToken}` } : {}) } });
  if (!response.ok) throw new Error(`Model service returned HTTP ${response.status}`);
  const reader = response.body.getReader();
  const chunks = []; let length = 0;
  while (true) {
    const { done, value } = await reader.read();
    if (done) break;
    length += value.length;
    if (length > 16384) { await reader.cancel(); throw new Error('Model response too large'); }
    chunks.push(value);
  }
  const text = Buffer.concat(chunks).toString('utf8');
  return applyBinaryTriage(c,validateVerdict(JSON.parse(text)));
}
