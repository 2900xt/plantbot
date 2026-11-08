// Scripted LeafLink demo: no GPU, no weights, no rover.
//
//   node scripts/demo.js            # http://localhost:8080, token printed on start
//   node scripts/demo.js --loop     # replay the row forever (expo table)
//
// A virtual rover (device rover-1) drives one row of six plants, streaming GPS and VOC
// samples, and uploads a photo at each plant. The photos are real PlantDoc test images
// (CC BY 4.0, downloaded into .demo-cache/ on first run, not committed). Leaf "segmentation"
// uses PlantDoc's annotated leaf boxes, and each leaf's verdict is its dataset label, so
// every run gives the same story. Verdicts are marked simulated, so the UI says DEMO.
import { mkdir, readFile, rm, writeFile, access } from 'node:fs/promises';
import { spawnSync } from 'node:child_process';
import path from 'node:path';
import { fileURLToPath } from 'node:url';
import sharp from 'sharp';
import { config } from '../server/config.js';
import { createPipeline } from '../server/pipeline.js';
import { createApp } from '../server/app.js';

const ROOT = path.resolve(path.dirname(fileURLToPath(import.meta.url)), '..');
const CACHE = path.join(ROOT, '.demo-cache');
const SOURCE = 'https://raw.githubusercontent.com/pratikkayal/PlantDoc-Object-Detection-Dataset/master/TEST/';
const TOKEN = 'leaflink-demo-token-local-only';
const ORIGIN = { lat: 40.80945, lon: -73.96025 }; // row start (Columbia, Morningside campus)
const SPACING_M = 3;
const loop = process.argv.includes('--loop');
const port = Number(process.argv[process.argv.indexOf('--port') + 1]) || 8080;

// The row, in driving order. boxes = PlantDoc annotations [xmin,ymin,xmax,ymax] in source pixels.
const PLANTS = [
  { file: 'DSCN1015.JPG.jpg', label: 'Tomato leaf', size: [1600, 1200] },
  { file: 'RnGnC.jpg', label: 'Bell_pepper leaf', size: [2147, 1432] },
  { file: '636227737977191231-septoria-leaf-spot-VT.jpg', label: 'Tomato Septoria leaf spot', size: [534, 401] },
  { file: '1684.jpg', label: 'Tomato leaf', size: [563, 631] },
  { file: '110822-206-Tomato-blight.jpg', label: 'Tomato leaf late blight', size: [750, 499] },
  { file: '00.jpg', label: 'Bell_pepper leaf spot', size: [480, 360] },
];
const diseased = label => !/^(Tomato|Bell_pepper) leaf$/.test(label);
const sleep = ms => new Promise(resolve => setTimeout(resolve, ms));
const say = msg => console.log(`\x1b[1;32m▶ ${msg}\x1b[0m`);

async function fetchCached(name, url = SOURCE + name) {
  const file = path.join(CACHE, name);
  try { return await readFile(file); } catch {}
  const response = await fetch(url, { signal: AbortSignal.timeout(60000) });
  if (!response.ok) throw new Error(`PlantDoc download failed for ${name}: HTTP ${response.status}`);
  const bytes = Buffer.from(await response.arrayBuffer());
  await writeFile(file, bytes);
  return bytes;
}

async function loadRow() {
  await mkdir(CACHE, { recursive: true });
  const labels = await fetchCached('test_labels.csv', new URL('../test_labels.csv', SOURCE).href).then(b => b.toString().trim().split('\n').slice(1));
  for (const plant of PLANTS) {
    plant.bytes = await fetchCached(plant.file);
    plant.boxes = labels.map(line => line.split(',')).filter(cols => cols[0] === plant.file)
      .map(cols => cols.slice(4, 8).map(Number));
    if (!plant.boxes.length) throw new Error(`No PlantDoc boxes for ${plant.file}`);
  }
}

// Pipeline overrides: what the GPU laptop's SegFormer + leaf heuristic + classifier would return.
let current = null;
async function extract(source, mask, onProgress) {
  const plant = current;
  onProgress({ stage: 'ground_mask', message: 'SegFormer-B2 is identifying ground regions' });
  await sleep(1200);
  onProgress({ stage: 'leaf_separation', message: 'Separating foliage into individual leaf candidates' });
  await sleep(1000);
  const meta = await sharp(source).metadata();
  const scale = meta.width / plant.size[0];
  const candidates = [];
  for (const [x0, y0, x1, y1] of plant.boxes) {
    const pad = 0.06 * Math.max(x1 - x0, y1 - y0);
    const left = Math.max(0, Math.round((x0 - pad) * scale)), top = Math.max(0, Math.round((y0 - pad) * scale));
    const width = Math.min(meta.width - left, Math.round((x1 - x0 + 2 * pad) * scale));
    const height = Math.min(meta.height - top, Math.round((y1 - y0 + 2 * pad) * scale));
    const bytes = await sharp(source).extract({ left, top, width, height })
      .resize(384, 384, { fit: 'contain', background: '#101410' }).jpeg({ quality: 90 }).toBuffer();
    candidates.push({ bytes, bounds: [x0, y0, x1, y1].map(v => Math.round(v * scale)),
      crop_bounds: [left, top, left + width, top + height], ground_overlap: 0.02 + 0.03 * Math.random() });
  }
  const ground = await sharp({ create: { width: meta.width, height: meta.height, channels: 3, background: '#000000' } }).toColourspace('b-w').png().toBuffer();
  return { candidates, ground_mask: ground, mask_source: 'segformer-b2-local', source_size: [meta.width, meta.height],
    crop_size: [384, 384], candidate_count: candidates.length, total_candidates: candidates.length, truncated: false,
    rejected_ground: 0, status: 'ok', quality: {}, heuristic: 'plantdoc-annotations', crop_mode: 'tight' };
}

async function infer(bytes, leafId) {
  await sleep(450);
  const plant = current;
  const sick = diseased(plant.label);
  return { verdict: sick ? 'diseased' : 'healthy', confidence: 0.86 + 0.11 * Math.random(),
    model_version: 'demo · PlantDoc labels', simulated: true, class_name: plant.label };
}

async function main() {
  await access(path.join(ROOT, 'dist', 'index.html')).catch(() => {
    say('building the LeafLink frontend (first run)');
    const built = spawnSync('npx', ['vite', 'build'], { cwd: ROOT, stdio: 'inherit' });
    if (built.status !== 0) throw new Error('vite build failed');
  });
  await loadRow();
  const dataDir = path.join(ROOT, 'runtime-demo');
  await rm(dataDir, { recursive: true, force: true });
  const c = config({ ...process.env, API_TOKEN: TOKEN, DATA_DIR: dataDir, PORT: String(port), HOST: process.env.HOST || '127.0.0.1',
    MODEL_MODE: 'demo', LEAF_CROP_MODE: 'heuristic', GROUND_MASK_MODE: 'local', STORAGE_DRIVER: 'local' });
  const logged = fn => async (...args) => {
    try { return await fn(...args); } catch (error) { console.error('demo stage failed:', error.message); throw error; }
  };
  const pipeline = await createPipeline(c, { extract: logged(extract), infer: logged(infer) });
  pipeline.field.updateSettings({ origin_lat: ORIGIN.lat, origin_lon: ORIGIN.lon, grid_meters: 2 });
  createApp(c, pipeline).listen(c.port, c.host);
  setInterval(() => pipeline.tick().catch(error => console.error('worker:', error.message)), 300);
  console.log(`\nLeafLink demo: http://localhost:${port}/   API token: ${TOKEN}\n`);

  let t = Date.now(), metres = 0, voc = null;
  const at = m => ({ lat: ORIGIN.lat + m / 111320, lon: ORIGIN.lon });
  // 1 Hz telemetry: position along the row, plant-side (top) and background (bottom) VOC.
  setInterval(() => {
    t = Math.max(t + 1000, Date.now());
    const measured_at = new Date(t).toISOString(), location = at(metres);
    const rise = voc ? voc.top : 0;
    pipeline.field.ingestLocation({ device: 'rover-1', measured_at, location });
    pipeline.field.ingest({ device: 'rover-1', measured_at, location, units: '100*ln(R0/R)',
      top: rise + 1.2 * (Math.random() - 0.5), bottom: rise * 0.15 + 1.0 * (Math.random() - 0.5) });
  }, 1000);

  do {
    await sleep(3000);
    say(`rover-1 starting down row 1: ${PLANTS.length} plants, ${SPACING_M} m apart`);
    for (const [i, plant] of PLANTS.entries()) {
      const target = (i + 1) * SPACING_M;
      while (metres < target) { metres = Math.min(target, metres + 0.5); await sleep(400); }
      say(`plant ${i + 1}: photo + sniff`);
      const parked = Date.now();
      if (diseased(plant.label)) voc = { top: 14 + 6 * Math.random() }; // plant-side smell while parked here
      current = plant;
      const row = await pipeline.submit(plant.bytes, 'rover-1', undefined, undefined,
        { captured_at: new Date().toISOString(), location: at(metres) });
      let done;
      while (!(done = pipeline.get(row.id)) || !['complete', 'failed', 'no_candidates'].includes(done.status)) await sleep(250);
      const r = done.result;
      say(`plant ${i + 1}: ${r ? `${r.diseased_count}/${r.leaf_count} leaves flagged (${plant.label})` : done.status}`);
      await sleep(Math.max(2500, 7000 - (Date.now() - parked)));
      voc = null;
    }
    say('end of row');
    if (loop) { await sleep(10000); metres = 0; }
  } while (loop);
  say('demo finished; LeafLink stays up (Ctrl-C to quit, --loop to repeat)');
}

process.on('SIGINT', () => process.exit(0));
main().catch(error => { console.error(error); process.exit(1); });
