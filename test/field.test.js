import test from 'node:test';
import assert from 'node:assert/strict';
import {mkdtemp,rm} from 'node:fs/promises';
import os from 'node:os';
import path from 'node:path';
import sharp from 'sharp';
import {config} from '../server/config.js';
import {createPipeline} from '../server/pipeline.js';
import {createApp} from '../server/app.js';
import {gridCell} from '../server/field.js';

async function fixture(t,overrides={}){
  const dir=await mkdtemp(path.join(os.tmpdir(),'leaf-field-'));let time=Date.now();
  const c=config({API_TOKEN:'field-test-token-123456789012345',DATA_DIR:dir,MODEL_MODE:'demo',LEAF_CROP_MODE:'heuristic'});
  const image=await sharp({create:{width:384,height:384,channels:3,background:'#318030'}}).jpeg().toBuffer();
  const mask=await sharp({create:{width:384,height:384,channels:3,background:'#000000'}}).toColourspace('b-w').png().toBuffer();
  const infer=async()=>({verdict:'diseased',confidence:.9,model_version:'fixture',simulated:false});
  const extract=async()=>({candidates:[1,2,3].map(i=>({bytes:image,bounds:[0,0,384,384]})),ground_mask:mask,status:'ok'});
  let p=await createPipeline(c,{now:()=>time,infer,extract,...overrides});
  t.after(async()=>{p.close();await rm(dir,{recursive:true,force:true});});
  return{c,image,get p(){return p;},advance(ms){time+=ms;},async restart(){p.close();p=await createPipeline(c,{now:()=>time,infer,extract,...overrides});}};
}
const at=i=>new Date(Date.now()-100000+i*1000).toISOString();
const sample=(i,top=120,bottom=100,extra={})=>({device:'pi',sample_id:`sample-${i}`,measured_at:at(i),top,bottom,units:'raw',location:{lat:.00001,lon:.00001},...extra});

test('image threshold is snapshotted; exact threshold creates one cached event; order persists',async t=>{
  const f=await fixture(t);
  f.p.field.updateSettings({infected_samples:3,cache_hours:.01});
  const row=await f.p.submit(f.image,'pi','image-key-0001',undefined,{location:{lat:.00001,lon:.00001}});
  f.p.field.updateSettings({infected_samples:4});
  await f.p.tick();let events=f.p.field.events();
  assert.equal(events.length,1);assert.equal(events[0].threshold,3);assert.equal(events[0].infected_samples,3);
  assert.equal(f.p.get(row.id).number,1);assert.ok(f.p.field.eventImage(events[0].id));
  const same=await f.p.submit(f.image,'pi','image-key-0001',undefined,{location:{lat:.00001,lon:.00001}});assert.equal(same.id,row.id);
  await assert.rejects(()=>f.p.submit(f.image,'pi','image-key-0001',undefined,{location:{lat:.00002,lon:.00001}}),e=>e.status===409);
  await f.restart();assert.equal(f.p.get(row.id).number,1);assert.equal(f.p.field.events().length,1);
  const below=await f.p.submit(f.image,'pi');await f.p.tick();assert.equal(below.number,2);assert.equal(f.p.field.events().length,1);
  f.advance(36001);assert.equal(f.p.field.eventImage(events[0].id),null);assert.equal(f.p.field.events().length,1);assert.equal(f.p.field.events()[0].cache_expires,null);
  assert.ok(await f.p.image(row.id));
});
test('sustained VOC uses fixed references, fires once, rearms after failure and survives restart',async t=>{
  const f=await fixture(t);f.p.field.updateSettings({voc_hold:3});
  f.p.field.ingest(sample(0,100));f.p.field.ingest(sample(1));
  await f.restart();f.p.field.ingest(sample(2));const [s]=f.p.field.ingest(sample(3));assert.ok(s.event_id);
  assert.equal(s.top_change,20);f.p.field.ingest(sample(4));assert.equal(f.p.field.events().length,1);
  f.p.field.ingest(sample(5,100));f.p.field.ingest([sample(6),sample(7),sample(8)]);assert.equal(f.p.field.events().length,2);
  assert.equal(f.p.field.overview().cells[0].voc,2);
});
test('duplicates, ordering, units, gaps, device isolation and atomic batch validation',async t=>{
  const f=await fixture(t);f.p.field.updateSettings({voc_hold:2});const baseline=sample(0,100);
  f.p.field.ingest(baseline);assert.equal(f.p.field.ingest(baseline)[0].duplicate,true);
  assert.throws(()=>f.p.field.ingest({...baseline,top:500}),e=>e.status===409);
  assert.throws(()=>f.p.field.ingest(sample(-1)));assert.throws(()=>f.p.field.ingest(sample(1,120,100,{units:'ppm'})));
  assert.throws(()=>f.p.field.ingest([sample(1),sample(2,NaN)]));assert.equal(f.p.field.overview().samples.length,1);
  f.p.field.ingest(sample(1));f.p.field.ingest(sample(20));assert.equal(f.p.field.events().length,0);
  f.p.field.ingest(sample(21));assert.equal(f.p.field.events().length,1);
  f.p.field.ingest(sample(22,120,100,{device:'other'}));assert.equal(f.p.field.events().length,1);
});
test('location stream joins by device and age; no invented position; history persists',async t=>{
  const f=await fixture(t);const measured_at=new Date().toISOString();const loc={device:'pi',measured_at,location:{lat:1,lon:1}};
  f.p.field.ingestLocation(loc);assert.equal(f.p.field.ingestLocation(loc).duplicate,true);
  const a=await f.p.submit(f.image,'pi');assert.equal(a.metadata.location_source,'device_stream');
  const b=await f.p.submit(f.image,'other');assert.equal(b.metadata.location,null);
  const c=await f.p.submit(f.image,'pi',undefined,undefined,{captured_at:at(0)});assert.equal(c.metadata.location,null);
  await f.restart();assert.equal(f.p.field.overview().location_history.length,1);
  f.p.field.ingestLocation({device:'pi',measured_at:new Date(Date.now()+2000).toISOString(),location:{lat:2,lon:2}});
  const delayed=await f.p.submit(f.image,'pi',undefined,undefined,{captured_at:measured_at});assert.deepEqual(delayed.metadata.location,{lat:1,lon:1});
  f.p.field.updateSettings({location_max_age_seconds:0});const later=new Date(Date.now()+1000).toISOString();
  const d=await f.p.submit(f.image,'pi',undefined,undefined,{captured_at:later});assert.equal(d.metadata.location,null);
});
test('VOC events can remain unmapped; grid changes and long episodes do not invent disease',async t=>{
  const f=await fixture(t);f.p.field.updateSettings({voc_hold:2});
  f.p.field.ingest([sample(0,100,100,{location:null}),sample(1,120,100,{location:null}),sample(2,120,100,{location:null})]);
  assert.equal(f.p.field.overview().unmapped,1);assert.equal(f.p.field.events()[0].kind,'voc');
  assert.throws(()=>f.p.field.updateSettings({infected_samples:0}));assert.throws(()=>f.p.field.updateSettings({voc_hold:1.5}));
  assert.throws(()=>f.p.field.updateSettings({constructor:1}));
  assert.deepEqual(gridCell({lat:-.00001,lon:-.00001},f.p.field.settings()),{x:-1,y:-1});
});
test('blur and missing CUDA never yield healthy or trigger an image event',async t=>{
  const f=await fixture(t,{extract:async()=>({candidates:[],ground_mask:Buffer.from('mask'),status:'retake_blurry'})});
  const a=await f.p.submit(f.image,'pi');await f.p.tick();assert.equal(f.p.get(a.id).status,'retake_blurry');assert.equal(f.p.get(a.id).result,null);
  assert.equal(f.p.field.events().length,0);
  const g=await fixture(t,{infer:async()=>{throw Object.assign(new Error('CUDA unavailable'),{code:'MODEL_UNAVAILABLE'});}});
  const b=await g.p.submit(g.image,'pi');await g.p.tick();assert.equal(g.p.get(b.id).status,'awaiting_model');assert.equal(g.p.field.events().length,0);
});
test('new API requires auth, rejects invalid metadata and serves expiring cached images',async t=>{
  const f=await fixture(t);f.p.field.updateSettings({infected_samples:1,cache_hours:.01});
  const server=createApp(f.c,f.p).listen(0,'127.0.0.1');await new Promise(r=>server.once('listening',r));
  t.after(async()=>{server.closeAllConnections();await new Promise(r=>server.close(r));});
  const origin=`http://127.0.0.1:${server.address().port}`;
  const get=(route,options={})=>fetch(origin+'/api'+route,{...options,headers:{Authorization:`Bearer ${f.c.token}`,...options.headers}});
  assert.equal((await fetch(origin+'/api/field')).status,401);
  assert.equal((await get('/settings')).status,200);
  const body=new FormData();body.append('image',new Blob([f.image]),'photo.jpg');body.append('metadata','{bad');
  assert.equal((await get('/captures',{method:'POST',body})).status,400);
  await f.p.submit(f.image,'pi');await f.p.tick();const e=f.p.field.events()[0];
  const image=await get(`/events/${e.id}/image`);assert.equal(image.status,200);assert.equal(image.headers.get('cache-control'),'no-store');
  f.advance(36001);assert.equal((await get(`/events/${e.id}/image`)).status,410);
  assert.equal((await get('/telemetry/voc',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(sample(0,100))})).status,202);
});

test('percentage rule uses infected sample share, exact boundary, snapshots and all-healthy rejection',async t=>{
  let infected=1,calls=0;
  const f=await fixture(t,{infer:async()=>({verdict:++calls<=infected?'diseased':'healthy',confidence:.8,model_version:'fixture',simulated:false})});
  f.p.field.updateSettings({image_rule:'percent',infected_percent:34});
  const first=await f.p.submit(f.image,'pi');await f.p.tick();assert.equal(f.p.field.events().length,0);
  assert.equal(f.p.get(first.id).result.diseased_count,1);
  f.p.field.updateSettings({infected_percent:100/3});calls=0;
  const boundary=await f.p.submit(f.image,'pi');f.p.field.updateSettings({infected_percent:80});await f.p.tick();
  const e=f.p.field.events()[0];assert.equal(e.capture_id,boundary.id);assert.equal(e.image_rule,'percent');assert.equal(e.infected_percent,100/3);assert.equal(e.threshold,100/3);
  infected=0;calls=0;f.p.field.updateSettings({infected_percent:1});await f.p.submit(f.image,'pi');await f.p.tick();assert.equal(f.p.field.events().length,1);
  assert.throws(()=>f.p.field.updateSettings({infected_percent:0}));assert.throws(()=>f.p.field.updateSettings({image_rule:'confidence'}));
});
