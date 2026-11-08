import {runPython} from './python.js';
import {gpuPredict} from './gpu.js';
export function validateVerdict(value) {
  if (!value || !['diseased', 'healthy'].includes(value.verdict) || typeof value.confidence !== 'number' || !Number.isFinite(value.confidence) || value.confidence < 0 || value.confidence > 1 || typeof value.model_version !== 'string' || !value.model_version.trim() || value.model_version.length > 200) throw new Error('Invalid model response');
  const result = { verdict: value.verdict, confidence: value.confidence, model_version: value.model_version, simulated: false };
  if (typeof value.class_name === 'string' && value.class_name.length <= 200) result.class_name=value.class_name;
  if (Number.isInteger(value.class_index) && value.class_index >= 0 && value.class_index < 38) result.class_index=value.class_index;
  if (Array.isArray(value.input_size) && value.input_size.length===2 && value.input_size.every(size=>Number.isInteger(size)&&size>0&&size<=1024)) result.input_size=value.input_size;
  if (value.score_type==='winning_class_softmax') result.score_type=value.score_type;
  if (Array.isArray(value.top_predictions) && value.top_predictions.length<=3 && value.top_predictions.every(p=>typeof p.class_name==='string'&&p.class_name.length<=200&&typeof p.confidence==='number'&&Number.isFinite(p.confidence)&&p.confidence>=0&&p.confidence<=1)) result.top_predictions=value.top_predictions;
  return result;
}
export async function infer(c, bytes, id) {
  if (c.modelMode === 'gpu') return validateVerdict(await gpuPredict(c,bytes));
  if (c.modelMode === 'demo') return { verdict: c.demoVerdict, confidence: 0.99, model_version: 'demo-no-ml', simulated: true };
  if (c.modelMode === 'local') return validateVerdict(await runPython(c,'predict_leaf.py',{image:bytes.toString('base64'),model_dir:c.localModelDir,cpu_threads:c.inferenceThreads,memory_format:c.inferenceFormat},c.modelTimeout));
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
  return validateVerdict(JSON.parse(text));
}
