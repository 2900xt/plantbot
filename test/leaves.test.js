import { test } from 'node:test';
import assert from 'node:assert/strict';
import { mkdtemp,rm } from 'node:fs/promises';
import { existsSync } from 'node:fs';
import path from 'node:path';
import os from 'node:os';
import sharp from 'sharp';
import { config } from '../server/config.js';
import { createPipeline } from '../server/pipeline.js';
import { createApp } from '../server/app.js';
import { storage } from '../server/storage.js';

async function fixture(t, settings = {}, overrides = {}) {
  const dir = await mkdtemp(path.join(os.tmpdir(),'leaflink-crops-'));
  // Keep the original v1 regression fixture; v2 blur/candidate behavior has separate coverage.
  const c = config({API_TOKEN:'leaf-tests-123456789012345678901',DATA_DIR:dir,GROUND_MASK_MODE:'provided',MODEL_MODE:'disabled',LEAF_HEURISTIC_VERSION:'v1',
    PYTHON_MODULE_PATH:process.env.PYTHON_MODULE_PATH || (existsSync('../.test-deps') ? path.resolve('../.test-deps') : undefined),...settings});
  const p = await createPipeline(c,overrides);
  const server = createApp(c,p).listen(0,'127.0.0.1');
  await new Promise(resolve=>server.once('listening',resolve));
  const request = (route,options={}) => fetch(`http://127.0.0.1:${server.address().port}/api${route}`,{...options,headers:{Authorization:`Bearer ${c.token}`,...options.headers}});
  t.after(async()=>{server.closeAllConnections();await new Promise(resolve=>server.close(resolve));p.close();await rm(dir,{recursive:true,force:true});});
  const width=640,height=400,data=Buffer.alloc(width*height*3,80);
  for (let y=0;y<height;y++) for (let x=0;x<width;x++) if (((x-180)/45)**2+((y-200)/75)**2<1 || ((x-460)/45)**2+((y-200)/75)**2<1) {
    const i=(y*width+x)*3;data[i]=30;data[i+1]=110+(x*17+y*23)%90;data[i+2]=20;
  }
  const image = await sharp(data,{raw:{width,height,channels:3}}).png().toBuffer();
  const mask = await sharp(Buffer.alloc(width*height),{raw:{width,height,channels:1}}).toColourspace('b-w').png().toBuffer();
  const upload = async (maskBytes=mask) => {
    const body=new FormData();body.append('image',new Blob([image]),'leaves.png');body.append('device','raspberry-pi');
    if (maskBytes) body.append('ground_mask',new Blob([maskBytes]),'mask.png');
    return request('/captures',{method:'POST',body});
  };
  return {c,p,image,mask,request,upload};
}

test('real heuristic after storage creates multiple 384px crops and individual model calls',async t=>{
  const calls=[];
  const f=await fixture(t,{MODEL_MODE:'http',MODEL_URL:'http://unused.invalid'}, {infer:async(bytes,id)=>{
    const meta=await sharp(bytes).metadata();assert.equal(meta.width,384);assert.equal(meta.height,384);
    calls.push(id);return {verdict:calls.length===1?'healthy':'diseased',confidence:.8,model_version:'fixture-v1',simulated:false};
  }});
  const row=await (await f.upload()).json();await f.p.tick();const done=f.p.get(row.id);
  assert.equal(done.status,'complete');assert.equal(done.leaves.length,2);assert.equal(calls.length,2);
  assert.equal(done.result.diseased_count,1);assert.equal(done.result.healthy_count,1);
  assert.equal(done.result.scope,'leaf_candidates');assert.equal(done.result.confidence,undefined);
  assert.equal(done.extraction.mask_source,'provided');
  for (const leaf of done.leaves) {
    assert.equal(leaf.object_key,undefined);assert.ok(leaf.bounds.length===4);assert.equal(leaf.ground_overlap,0);
    const response=await f.request(`/captures/${row.id}/leaves/${leaf.id}/image`);assert.equal(response.status,200);
    assert.equal(response.headers.get('cache-control'),'no-store');
    assert.equal((await sharp(Buffer.from(await response.arrayBuffer())).metadata()).width,384);
  }
  assert.equal((await f.request(`/captures/${row.id}/leaves/missing/image`)).status,404);
});

test('missing mask waits; attached mask splits even with disease model disabled',async t=>{
  const f=await fixture(t);
  const row=await (await f.upload(null)).json();await f.p.tick();
  assert.equal(f.p.get(row.id).status,'awaiting_mask');assert.equal(f.p.get(row.id).result,null);
  const body=new FormData();body.append('ground_mask',new Blob([f.mask]),'mask.png');
  assert.equal((await f.request(`/captures/${row.id}/ground-mask`,{method:'POST',body})).status,202);
  await f.p.tick();const waiting=f.p.get(row.id);
  assert.equal(waiting.status,'awaiting_model');assert.equal(waiting.leaves.length,2);
  assert.ok(waiting.leaves.every(leaf=>leaf.result===null));assert.equal(waiting.result,null);
});

test('all-ground mask produces no_candidates with no model verdict',async t=>{
  let calls=0;
  const f=await fixture(t,{MODEL_MODE:'demo'},{infer:()=>{calls++;throw new Error('must not call');}});
  const mask=await sharp(Buffer.alloc(640*400,255),{raw:{width:640,height:400,channels:1}}).toColourspace('b-w').png().toBuffer();
  const row=await (await f.upload(mask)).json();await f.p.tick();const done=f.p.get(row.id);
  assert.equal(done.status,'no_candidates');assert.equal(done.result,null);assert.equal(calls,0);assert.equal(done.leaves.length,0);
  assert.equal(done.extraction.rejected_ground,2);
});

test('nonbinary, multiclass/color and misaligned masks fail before capture is accepted',async t=>{
  const f=await fixture(t);
  const invalid=await sharp(Buffer.alloc(640*400,2),{raw:{width:640,height:400,channels:1}}).toColourspace('b-w').png().toBuffer();
  assert.equal((await f.upload(invalid)).status,400);
  assert.equal((await f.upload(f.image)).status,400);
  const small=await sharp(Buffer.alloc(100),{raw:{width:10,height:10,channels:1}}).toColourspace('b-w').png().toBuffer();
  assert.equal((await f.upload(small)).status,400);assert.equal(f.p.list().length,0);
});

test('partial per-leaf results survive retry; completed leaves are not reclassified',async t=>{
  let now=Date.now(),calls=[],failed=false;
  const f=await fixture(t,{MODEL_MODE:'demo'},{now:()=>now,infer:async(bytes,id)=>{
    calls.push(id);
    if(id.endsWith('02')&&!failed){failed=true;throw new Error('temporary model outage');}
    return {verdict:'healthy',confidence:.9,model_version:'retry-fixture',simulated:false};
  }});
  const row=await (await f.upload()).json();await f.p.tick();
  let partial=f.p.get(row.id);assert.equal(partial.status,'queued');assert.equal(partial.leaves[0].status,'complete');assert.equal(partial.result,null);
  now+=10000;await f.p.tick();const done=f.p.get(row.id);
  assert.equal(done.status,'complete');assert.equal(calls.length,3);assert.equal(calls.filter(id=>id.endsWith('01')).length,1);
});

test('v2 relaxed blur policy is passed to extraction and still blocks severe defocus',async t=>{
  let calls=0;
  const f=await fixture(t,{LEAF_HEURISTIC_VERSION:'v2',MODEL_MODE:'demo'}, {infer:()=>{calls++;throw new Error('Blurred control must not reach classifier');}});
  const blurred=await sharp(f.image).blur(12).png().toBuffer();
  const row=await f.p.submit(blurred,'blur-control',undefined,f.mask);
  await f.p.tick();const done=f.p.get(row.id);
  assert.equal(done.status,'retake_blurry');assert.equal(done.result,null);assert.equal(calls,0);
  assert.equal(done.extraction.quality.blur_threshold,.00025);
  assert.equal(done.extraction.quality.crop_sharpness_threshold,.0001);
});

test('blur policy accepts explicit rollback settings and rejects invalid thresholds',()=>{
  const env={API_TOKEN:'blur-policy-test-token-1234567890'};
  const c=config({...env,LEAF_BLUR_THRESHOLD:'.0025',MIN_CROP_SHARPNESS:'.001'});
  assert.equal(c.blurThreshold,.0025);assert.equal(c.cropSharpnessThreshold,.001);
  for(const value of ['NaN','Infinity','-0.01','1.01']){
    assert.throws(()=>config({...env,LEAF_BLUR_THRESHOLD:value}),/Blur thresholds/);
    assert.throws(()=>config({...env,MIN_CROP_SHARPNESS:value}),/Blur thresholds/);
  }
});
