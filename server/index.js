import { config } from './config.js';
import { createPipeline } from './pipeline.js';
import { createApp } from './app.js';
const c = config();
const pipeline = await createPipeline(c);
const server = createApp(c, pipeline).listen(c.port, c.host, () => console.log(`LeafLink listening on ${c.host}:${c.port}; storage=${c.driver}; model=${c.modelMode}`));
const timer = setInterval(() => pipeline.tick().catch(() => console.error('Worker could not access job database')), 500);
let stopping = false;
async function stop() {
  if (stopping) return;
  stopping = true;
  clearInterval(timer);
  // Let pending requests and the current job finish before closing SQLite.
  await new Promise(resolve => server.close(resolve));
  // A running worker can still be in a network request. Leave its durable state
  // for restart recovery rather than closing the DB underneath it.
  process.exit(0);
}
process.on('SIGINT', stop);
process.on('SIGTERM', stop);
