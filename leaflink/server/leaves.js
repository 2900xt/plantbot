import { runPython } from './python.js';
import {residentExtract} from './leaf-worker.js';
export {closeLeafWorker} from './leaf-worker.js';

export async function extractLeaves(c, image, mask, onProgress) {
  const payload = { image: image.toString('base64'), mask: mask?.toString('base64'),
    ground_mode: c.groundMode, ground_model_dir: c.groundModelDir,
    crop_mode: c.cropMode, min_leaf_extent: c.minLeafExtent, max_candidates: c.maxCandidates,heuristic_version:c.leafVersion,
    blur_threshold:c.blurThreshold,crop_sharpness_threshold:c.cropSharpnessThreshold,
    ground_device:c.groundDevice || 'cpu',ground_cpu_threads:c.groundCpuThreads || 4 };
  const result = c.residentLeafWorker === false
    ? await runPython(c,'process_leaves.py',payload,c.cropTimeout,onProgress)
    : await residentExtract(c,payload,onProgress);
  if (!Array.isArray(result.candidates) || result.candidates.length > c.maxCandidates || !result.ground_mask) throw new Error('Invalid leaf extraction output');
  return { ...result, ground_mask:Buffer.from(result.ground_mask,'base64'),
    candidates:result.candidates.map(({image,...candidate}) => ({...candidate,bytes:Buffer.from(image,'base64')})) };
}
