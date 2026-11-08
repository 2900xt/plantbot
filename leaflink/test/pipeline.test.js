import { test } from 'node:test';
import assert from 'node:assert/strict';
import { mkdtemp, rm } from 'node:fs/promises';
import os from 'node:os';
import path from 'node:path';
import http from 'node:http';
import sharp from 'sharp';
import { createPipeline } from '../server/pipeline.js';
import { createApp } from '../server/app.js';
import { config } from '../server/config.js';
import { infer, validateVerdict } from '../server/model.js';
import { storage } from '../server/storage.js';

const token = 'test-only-token-12345678901234567890';
async function fixture(t, changes = {}, overrides = {}) {
  const dir = await mkdtemp(path.join(os.tmpdir(), 'leaflink-test-'));
  const c = config({ API_TOKEN: token, DATA_DIR: dir, MODEL_MODE: 'demo', LEAF_CROP_MODE:'disabled', ...changes });
  const pipeline = await createPipeline(c, overrides);
  const app = createApp(c, pipeline);
  const server = app.listen(0, '127.0.0.1');
  await new Promise(resolve => server.once('listening', resolve));
  const url = `http://127.0.0.1:${server.address().port}`;
  t.after(async () => { server.closeAllConnections(); await new Promise(resolve => server.close(resolve)); pipeline.close(); await rm(dir, {recursive:true,force:true}); });
  const bytes = await sharp({ create: { width: 60, height: 40, channels: 3, background: '#3c8b47' } }).png().toBuffer();
  const request = (route, opts = {}) => fetch(url + route, { ...opts, headers: { Authorization: `Bearer ${token}`, ...opts.headers } });
  const upload = (image = bytes, key = 'capture-key-12345', device = 'raspberry-pi') => {
    const body = new FormData(); body.append('image', new Blob([image],{type:'image/png'}), 'leaf.png'); body.append('device',device);
    return request('/api/captures', {method:'POST',body,headers:{'Idempotency-Key':key}});
  };
  return { c, pipeline, request, upload, bytes, url };
}

test('HTTP flow: authenticated upload → stored image → simulated verdict → history', async t => {
  const f = await fixture(t);
  assert.equal((await fetch(f.url + '/api/captures')).status,401);
  assert.equal((await fetch(f.url + '/api/health')).status,200);
  const response = await f.upload(); assert.equal(response.status,202);
  const row = await response.json(); assert.equal(row.status,'queued');
  await f.pipeline.tick();
  const result = await (await f.request(`/api/captures/${row.id}`)).json();
  assert.equal(result.status,'complete'); assert.equal(result.result.verdict,'diseased');
  assert.equal(result.result.simulated,true); assert.equal(result.stored,true);
  const image = await f.request(`/api/captures/${row.id}/image`); assert.equal(image.headers.get('content-type'),'image/jpeg');
  assert.equal((await sharp(Buffer.from(await image.arrayBuffer())).metadata()).width,60);
  assert.equal((await (await f.request('/api/captures')).json()).length,1);
  assert.equal((await f.request('/api/captures/missing')).status,404);
});

test('idempotent and concurrent requests create one capture; changed content/device rejected', async t => {
  const f = await fixture(t);
  const responses = await Promise.all([f.upload(),f.upload(),f.upload()]);
  const rows = await Promise.all(responses.map(r => r.json()));
  assert.equal(new Set(rows.map(r => r.id)).size,1);
  assert.equal(f.pipeline.list().length,1);
  assert.equal((await f.upload(f.bytes,'capture-key-12345','phone')).status,409);
  const other = await sharp({create:{width:10,height:10,channels:3,background:'#ffffff'}}).png().toBuffer();
  assert.equal((await f.upload(other)).status,409);
});

test('reject invalid images, oversized uploads, bad keys and unsafe metadata', async t => {
  const f = await fixture(t);
  assert.equal((await f.upload(Buffer.from('not an image'))).status,400);
  assert.equal((await f.upload(f.bytes,'!bad')).status,400);
  assert.equal((await f.upload(f.bytes,'capture-key-12345','<script>')).status,400);
  assert.equal((await f.upload(Buffer.alloc(12*1024*1024+1))).status,413);
  assert.equal(f.pipeline.list().length,0);
});

test('disabled model stores first, survives restart and can later retry into a verdict', async t => {
  const dir = await mkdtemp(path.join(os.tmpdir(),'leaflink-recovery-'));
  let p;
  t.after(async () => { p?.close(); await rm(dir,{recursive:true,force:true}); });
  const c = config({API_TOKEN:token,DATA_DIR:dir,MODEL_MODE:'disabled',LEAF_CROP_MODE:'disabled'});
  p = await createPipeline(c);
  const bytes = await sharp({create:{width:20,height:20,channels:3,background:'green'}}).jpeg().toBuffer();
  const row = await p.submit(bytes,'phone','restart-test-key');
  await p.tick(); assert.equal(p.get(row.id).status,'awaiting_model'); assert.equal(p.get(row.id).result,null);
  p.close(); p = await createPipeline({...c,modelMode:'demo'});
  assert.equal(p.get(row.id).status,'awaiting_model');
  p.retry(row.id); await p.tick(); assert.equal(p.get(row.id).status,'complete');
});

test('transient storage failure backs off; retry succeeds and model sees storage bytes', async t => {
  let now = Date.now(), puts = 0, stored, reads = 0;
  const f = await fixture(t, {}, { now: () => now, storage: {
    async put(key, bytes) { if (++puts === 1) throw new Error('outage'); stored = bytes; },
    async get() { reads++; return stored; },
  }, infer: async bytes => { assert.deepEqual(bytes,stored); return {verdict:'healthy',confidence:.8,model_version:'fixture',simulated:false}; } });
  const row = await (await f.upload()).json(); await f.pipeline.tick();
  assert.equal(f.pipeline.get(row.id).status,'queued'); assert.equal(f.pipeline.get(row.id).attempts,1);
  await f.pipeline.tick(); assert.equal(puts,1);
  now += 10000; await f.pipeline.tick();
  assert.equal(f.pipeline.get(row.id).status,'complete'); assert.equal(reads,1);
});

test('permanent model failures stop after three attempts; manual retry is available', async t => {
  let now = Date.now();
  const f = await fixture(t,{}, {now:() => now,infer:async () => {throw new Error('offline');}});
  const row = await (await f.upload()).json();
  for (let i=0;i<3;i++) {await f.pipeline.tick(); now+=10000;}
  assert.equal(f.pipeline.get(row.id).status,'failed'); assert.equal(f.pipeline.get(row.id).attempts,3);
  assert.equal((await f.request(`/api/captures/${row.id}/retry`,{method:'POST'})).status,202);
  assert.equal((await f.request(`/api/captures/${row.id}/retry`,{method:'POST'})).status,409);
});

test('HTTP model multipart contract and strict response validation', async t => {
  const f = await fixture(t);
  const server = http.createServer(async (req,res) => {
    assert.equal(req.headers.authorization,'Bearer model-secret');
    assert.equal(req.headers['x-capture-id'],'fixture-id');
    const parts = []; for await (const part of req) parts.push(part);
    assert.match(Buffer.concat(parts).toString('latin1'),/name="image"/);
    res.setHeader('Content-Type','application/json'); res.end(JSON.stringify({verdict:'healthy',confidence:.87,model_version:'v1'}));
  }).listen(0,'127.0.0.1');
  await new Promise(resolve => server.once('listening',resolve));
  t.after(() => {server.closeAllConnections();server.close();});
  const result = await infer({...f.c,modelMode:'http',modelUrl:`http://127.0.0.1:${server.address().port}`,modelToken:'model-secret'},f.bytes,'fixture-id');
  assert.deepEqual(result,{verdict:'healthy',confidence:.87,model_version:'v1',simulated:false});
  for (const bad of [{}, {verdict:'unknown',confidence:.5,model_version:'v1'}, {verdict:'diseased',confidence:2,model_version:'v1'}, {verdict:'healthy',confidence:'0.9',model_version:'v1'}]) assert.throws(() => validateVerdict(bad));
});

test('B2 adapter sends signed S3 PUT and GET, preserves stored bytes', async t => {
  let stored, puts=0, gets=0;
  const server = http.createServer(async (req,res) => {
    assert.match(req.headers.authorization,/AWS4-HMAC-SHA256/);
    assert.equal(req.url,'/test-bucket/captures/test.jpg?x-id='+(req.method === 'PUT' ? 'PutObject' : 'GetObject'));
    if (req.method === 'PUT') {puts++;const chunks=[];for await (const c of req) chunks.push(c);stored=Buffer.concat(chunks);res.setHeader('ETag','"fixture"');res.end();}
    else {gets++;res.setHeader('Content-Type','image/jpeg');res.end(stored);}
  }).listen(0,'127.0.0.1');
  await new Promise(resolve => server.once('listening',resolve));
  t.after(() => {server.closeAllConnections();server.close();});
  const adapter = storage({driver:'b2',endpoint:`http://127.0.0.1:${server.address().port}`,region:'us-west-004',bucket:'test-bucket',keyId:'fixture',appKey:'fixture'});
  await adapter.put('captures/test.jpg',Buffer.from('storage-fixture'));
  assert.equal((await adapter.get('captures/test.jpg')).toString(),'storage-fixture'); assert.equal(puts,1);assert.equal(gets,1);
});

test('configuration fails closed for missing credentials and model settings', () => {
  assert.throws(() => config({}));
  assert.throws(() => config({API_TOKEN:token,STORAGE_DRIVER:'b2'}));
  assert.throws(() => config({API_TOKEN:token,MODEL_MODE:'http'}));
  assert.throws(() => config({API_TOKEN:token,INFERENCE_CPU_THREADS:'0'}));
  assert.throws(() => config({API_TOKEN:token,INFERENCE_MEMORY_FORMAT:'invalid'}));
  const optimized=config({API_TOKEN:token});
  assert.equal(optimized.inferenceThreads,8);assert.equal(optimized.inferenceFormat,'channels_last');
  const original=config({API_TOKEN:token,INFERENCE_CPU_THREADS:'4',INFERENCE_MEMORY_FORMAT:'contiguous'});
  assert.equal(original.inferenceThreads,4);assert.equal(original.inferenceFormat,'contiguous');
});
