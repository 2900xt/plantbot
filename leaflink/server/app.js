import express from 'express';
import cors from 'cors';
import multer from 'multer';
import { rateLimit } from 'express-rate-limit';
import { timingSafeEqual } from 'node:crypto';
import { fileURLToPath } from 'node:url';
export function createApp(c, pipeline) {
  const app = express();
  app.disable('x-powered-by');
  app.use((req, res, next) => {
    res.set({ 'X-Content-Type-Options': 'nosniff', 'Referrer-Policy': 'no-referrer',
      'Content-Security-Policy': "default-src 'self'; script-src 'self'; style-src 'self'; img-src 'self' blob: data: http: https:; connect-src 'self' https: http: ws: wss:; object-src 'none'; base-uri 'self'; frame-ancestors 'none'" });
    next();
  });
  app.use(cors({ origin(origin, callback) { callback(null, !origin || c.origins.includes(origin)); }, allowedHeaders: ['Authorization','Content-Type','Idempotency-Key'] }));
  app.get('/api/health', (req,res) => res.json({ ok: true,server_time:new Date().toISOString() }));
  app.use('/api', (req,res,next) => {
    res.set('Cache-Control', 'no-store');
    const supplied = Buffer.from(req.headers.authorization || '');
    const expected = Buffer.from(`Bearer ${c.token}`);
    if (supplied.length !== expected.length || !timingSafeEqual(supplied, expected)) return res.status(401).json({ error: 'Invalid API token' });
    next();
  });
  app.use('/api', rateLimit({ windowMs: 60000, limit: 180, standardHeaders: 'draft-8', legacyHeaders: false }));
  app.use('/api',express.json({limit:'512kb'}));
  app.get('/api/field', (req,res)=>res.json(pipeline.field.overview()));
  app.get('/api/events', (req,res)=>res.json(pipeline.field.events()));
  app.get('/api/events/:id/image', (req,res)=>{
    const bytes=pipeline.field.eventImage(req.params.id);
    if(!bytes) return res.status(410).json({error:'Event photo cache expired or image unavailable'});
    res.type('jpeg').send(Buffer.from(bytes));
  });
  app.get('/api/settings', (req,res)=>res.json(pipeline.field.settings()));
  app.put('/api/settings', (req,res)=>res.json(pipeline.field.updateSettings(req.body)));
  app.post('/api/telemetry/voc', (req,res)=>res.status(202).json(pipeline.field.ingest(req.body)));
  app.post('/api/telemetry/location', (req,res)=>res.status(202).json(pipeline.field.ingestLocation(req.body)));
  app.post('/api/telemetry/reset', (req,res)=>res.json(pipeline.field.resetBaseline(req.body?.device)));
  app.get('/api/config', (req,res) => res.json({ storage: c.driver, model: c.modelMode, leaf_extraction:c.leafMode, ground_mask:c.groundMode,
    binary_triage:c.binaryTriage && c.modelMode!=='experts', binary_policy:c.modelMode==='experts'?{kind:'selected_expert_sigmoid',disease_threshold:.5}:c.binaryTriage?{disease_threshold:c.binaryDiseaseThreshold,provisional:true}:null }));
  app.get('/api/captures', (req,res) => res.json(pipeline.list()));
  const upload = multer({ storage: multer.memoryStorage(), limits: { fileSize: 12 * 1024 * 1024, files: 2, fields: 2, fieldSize: 4096, parts: 4 } });
  app.post('/api/captures', rateLimit({ windowMs: 60000, limit: 20, legacyHeaders: false }), upload.fields([{name:'image',maxCount:1},{name:'ground_mask',maxCount:1}]), async (req,res) => {
    if (!req.files?.image?.[0]) return res.status(400).json({ error: 'Multipart field image is required' });
    let metadata={};
    try {metadata=req.body.metadata ? JSON.parse(req.body.metadata):{};} catch {return res.status(400).json({error:'metadata must contain valid JSON'});}
    res.status(202).json(await pipeline.submit(req.files.image[0].buffer, req.body.device || 'phone', req.get('Idempotency-Key'),req.files.ground_mask?.[0]?.buffer,metadata));
  });
  app.get('/api/captures/:id', (req,res) => { const row = pipeline.get(req.params.id); res.status(row ? 200 : 404).json(row || { error: 'Capture not found' }); });
  app.post('/api/captures/:id/retry', (req,res) => { const row = pipeline.retry(req.params.id); res.status(row ? 202 : 404).json(row || { error: 'Capture not found' }); });
  app.post('/api/captures/:id/ground-mask',upload.single('ground_mask'),async (req,res) => {
    if (!req.file) return res.status(400).json({error:'Multipart field ground_mask is required'});
    const row = await pipeline.attachMask(req.params.id,req.file.buffer);
    res.status(row ? 202 : 404).json(row || {error:'Capture not found'});
  });
  app.get('/api/captures/:id/leaves/:leafId/image',async (req,res) => {
    const bytes = await pipeline.leafImage(req.params.id,req.params.leafId);
    if (!bytes) return res.status(404).json({error:'Leaf candidate not found'});
    res.type('jpeg').send(bytes);
  });
  app.get('/api/captures/:id/image', async (req,res) => {
    const bytes = await pipeline.image(req.params.id);
    if (!bytes) return res.status(404).json({ error: 'Capture not found' });
    res.type('jpeg').send(bytes);
  });
  app.use('/api', (req,res) => res.status(404).json({ error: 'Unknown API route' }));
  app.use(express.static(fileURLToPath(new URL('../dist/', import.meta.url)), { maxAge: 0 }));
  app.use((error,req,res,next) => {
    const status = error instanceof multer.MulterError ? (error.code === 'LIMIT_FILE_SIZE' ? 413 : 400) : error.status || 500;
    res.status(status).json({ error: status === 500 ? 'Unexpected server error' : error.message });
  });
  return app;
}
