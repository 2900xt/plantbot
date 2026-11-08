import path from 'node:path';
export function config(env = process.env) {
  const c = {
    host: env.HOST || '127.0.0.1', port: Number(env.PORT || 8080),
    token: env.API_TOKEN || '', origins: (env.CORS_ORIGINS || 'http://localhost:5173,https://localhost,capacitor://localhost').split(',').map(s => s.trim()),
    dataDir: path.resolve(env.DATA_DIR || './runtime'), driver: env.STORAGE_DRIVER || 'local',
    endpoint: env.B2_ENDPOINT, region: env.B2_REGION, bucket: env.B2_BUCKET,
    keyId: env.B2_KEY_ID, appKey: env.B2_APPLICATION_KEY,
    modelMode: env.MODEL_MODE || 'disabled', modelUrl: env.MODEL_URL, modelToken: env.MODEL_TOKEN,
    binaryTriage: env.BINARY_TRIAGE === '1',
    binaryDiseaseThreshold: Number(env.BINARY_DISEASE_THRESHOLD ?? .8),
    modelTimeout: Number(env.MODEL_TIMEOUT_MS || (env.MODEL_MODE === 'local' ? 120000 : 30000)), demoVerdict: env.DEMO_VERDICT || 'diseased',
    localModelDir: path.resolve(env.LOCAL_MODEL_DIR || '../outputs/models/leaf-cnn-epoch6'),
    gpuModelDir:path.resolve(env.GPU_MODEL_DIR || './models/LeafCNN-Epoch8-GPU'),
    segmentedModelDir:path.resolve(env.SEGMENTED_MODEL_DIR || './models/LeafCNN-Field-Checkpoint1'),
    segmentationModelDir:path.resolve(env.SEGMENTATION_MODEL_DIR || './models/SAM2.1-Hiera-Tiny'),
    segmentedDevice:env.SEGMENTED_DEVICE || 'auto',segmentationCpuThreads:Number(env.SEGMENTATION_CPU_THREADS || 4),
    expertModelDir:path.resolve(env.EXPERT_MODEL_DIR || './models/CropExperts-ResNet18'),
    expertDevice:env.EXPERT_DEVICE || 'auto',
    leafVersion:env.LEAF_HEURISTIC_VERSION || 'v2',
    inferenceThreads:Number(env.INFERENCE_CPU_THREADS || 8),inferenceFormat:env.INFERENCE_MEMORY_FORMAT || 'channels_last',
    leafMode: env.LEAF_CROP_MODE || 'heuristic', groundMode: env.GROUND_MASK_MODE || 'local',
    residentLeafWorker:env.RESIDENT_LEAF_WORKER !== '0',groundDevice:env.GROUND_DEVICE || 'cpu',
    groundCpuThreads:Number(env.GROUND_CPU_THREADS || 4),
    groundModelDir: path.resolve(env.GROUND_MODEL_DIR || './models/PlantLeafPipeline/models/segformer-b2-ade20k'),
    pythonBin: env.PYTHON_BIN || (process.platform === 'win32' ? 'py' : 'python3'), pythonModulePath: env.PYTHON_MODULE_PATH,
    cropMode: env.LEAF_WINDOW_MODE || 'tight', minLeafExtent: Number(env.MIN_LEAF_EXTENT || 80),
    maxCandidates: Number(env.MAX_LEAF_CANDIDATES || 40), cropTimeout: Number(env.CROP_TIMEOUT_MS || 120000),
    blurThreshold:Number(env.LEAF_BLUR_THRESHOLD || .00025),cropSharpnessThreshold:Number(env.MIN_CROP_SHARPNESS || .0001),
  };
  if (c.token.length < 24 || c.token.startsWith('replace-')) throw new Error('Set API_TOKEN to a random secret of at least 24 characters.');
  if (!['local', 'b2'].includes(c.driver)) throw new Error('Invalid STORAGE_DRIVER');
  if (c.driver === 'b2' && ![c.endpoint,c.region,c.bucket,c.keyId,c.appKey].every(Boolean)) throw new Error('All B2 settings are required');
  if (c.driver === 'b2' && (!/^https:\/\/s3\.[a-z0-9-]+\.backblazeb2\.com$/.test(c.endpoint))) throw new Error('Use the HTTPS B2 regional S3 endpoint');
  if (!['disabled','http','demo','local','gpu','experts','segmented'].includes(c.modelMode)) throw new Error('Invalid MODEL_MODE');
  if (!['auto','cpu','cuda'].includes(c.segmentedDevice) || !Number.isInteger(c.segmentationCpuThreads) || c.segmentationCpuThreads<1 || c.segmentationCpuThreads>32) throw new Error('Invalid segmented runtime settings');
  if (!['auto','cpu','cuda'].includes(c.expertDevice)) throw new Error('EXPERT_DEVICE must be auto, cpu or cuda');
  if (![undefined,'0','1'].includes(env.BINARY_TRIAGE) || !Number.isFinite(c.binaryDiseaseThreshold) || c.binaryDiseaseThreshold < 0 || c.binaryDiseaseThreshold > 1) throw new Error('Binary threshold must be between 0 and 1 and BINARY_TRIAGE=0 or 1');
  if (!['v1','v2'].includes(c.leafVersion)) throw new Error('Invalid LEAF_HEURISTIC_VERSION');
  if (!['cpu','cuda'].includes(c.groundDevice) || !Number.isInteger(c.groundCpuThreads) || c.groundCpuThreads<1 || c.groundCpuThreads>32) throw new Error('Invalid ground runtime settings');
  if (c.modelMode === 'http' && !/^https?:\/\//.test(c.modelUrl || '')) throw new Error('MODEL_URL is required');
  if (!Number.isFinite(c.modelTimeout) || c.modelTimeout < 100 || c.modelTimeout > 300000) throw new Error('Invalid MODEL_TIMEOUT_MS');
  if (!['diseased','healthy'].includes(c.demoVerdict)) throw new Error('Invalid DEMO_VERDICT');
  if (!Number.isInteger(c.inferenceThreads) || c.inferenceThreads<1 || c.inferenceThreads>32 || !['channels_last','contiguous'].includes(c.inferenceFormat)) throw new Error('Invalid inference runtime settings');
  if (!['heuristic','disabled'].includes(c.leafMode) || !['local','provided'].includes(c.groundMode) || !['tight','fixed'].includes(c.cropMode)) throw new Error('Invalid leaf extraction mode');
  if (!Number.isInteger(c.minLeafExtent) || c.minLeafExtent < 1 || !Number.isInteger(c.maxCandidates) || c.maxCandidates < 1 || c.maxCandidates > 64 || !Number.isFinite(c.cropTimeout) || c.cropTimeout < 100 || c.cropTimeout > 600000) throw new Error('Invalid leaf extraction limits');
  if (![c.blurThreshold,c.cropSharpnessThreshold].every(value=>Number.isFinite(value)&&value>=0&&value<=1)) throw new Error('Blur thresholds must be finite numbers from 0 to 1');
  return c;
}
