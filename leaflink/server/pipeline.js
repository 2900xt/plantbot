import { DatabaseSync } from 'node:sqlite';
import { mkdir, readFile, writeFile, unlink } from 'node:fs/promises';
import path from 'node:path';
import { randomUUID, createHash } from 'node:crypto';
import sharp from 'sharp';
import { storage } from './storage.js';
import { infer } from './model.js';
import { extractLeaves } from './leaves.js';
import { createFieldStore } from './field.js';

async function normalizeMask(bytes, sourceSize, storedSize) {
  try {
    const image = sharp(bytes,{limitInputPixels:40000000,failOn:'warning'});
    const meta = await image.metadata();
    if (meta.format !== 'png' || meta.channels !== 1 || (meta.pages || 1) !== 1 || meta.width !== sourceSize[0] || meta.height !== sourceSize[1]) throw new Error();
    const pixels = await image.raw().toBuffer();
    if (!pixels.every(value => value === 0 || value === 1 || value === 255)) throw new Error();
    // Canonicalize 1 and 255 to the same ignore value before nearest resize.
    const binary = Buffer.from(pixels);
    for (let i=0;i<binary.length;i++) binary[i] = pixels[i] ? 255 : 0;
    return await sharp(binary,{raw:{width:meta.width,height:meta.height,channels:1}})
      .resize(storedSize[0],storedSize[1],{kernel:'nearest'}).toColourspace('b-w').png().toBuffer();
  } catch { throw Object.assign(new Error('ground_mask must be a single-channel binary PNG (0 keep; 1/255 ignore) matching the EXIF-oriented source image'),{status:400}); }
}

export async function createPipeline(c, overrides = {}) {
  await mkdir(path.join(c.dataDir, 'spool'), { recursive: true });
  const db = new DatabaseSync(path.join(c.dataDir, 'jobs.sqlite'));
  db.exec(`PRAGMA journal_mode=WAL; CREATE TABLE IF NOT EXISTS captures (
    id TEXT PRIMARY KEY, device TEXT NOT NULL, created TEXT NOT NULL, status TEXT NOT NULL,
    object_key TEXT NOT NULL, digest TEXT NOT NULL, idempotency TEXT UNIQUE,
    attempts INTEGER NOT NULL DEFAULT 0, next_attempt INTEGER NOT NULL DEFAULT 0,
    result TEXT, error TEXT, stored INTEGER NOT NULL DEFAULT 0
  )`);
  const columns = new Set(db.prepare('PRAGMA table_info(captures)').all().map(row => row.name));
  for (const [name,type] of Object.entries({field_metadata:'TEXT',extraction:'TEXT',leaves:'TEXT',mask_key:'TEXT',mask_digest:'TEXT',mask_stored:'INTEGER NOT NULL DEFAULT 0',dimensions:'TEXT',progress:'TEXT',started:'TEXT'})) {
    if (!columns.has(name)) db.exec(`ALTER TABLE captures ADD COLUMN ${name} ${type}`);
  }
  // One API/worker process per DATA_DIR. Recover interrupted jobs after restart.
  db.exec("UPDATE captures SET status='queued' WHERE status IN ('uploading','splitting','processing')");
  const objects = overrides.storage || storage(c);
  const now = overrides.now || Date.now;
  const field = createFieldStore(db,now);
  const predict = overrides.infer || ((bytes, id) => infer(c, bytes, id));
  const split = overrides.extract || ((bytes, mask, onProgress) => extractLeaves(c, bytes, mask, onProgress));
  const progress = (id,stage,message,extra={}) => db.prepare('UPDATE captures SET progress=? WHERE id=?').run(JSON.stringify({stage,message,updated_at:new Date().toISOString(),...extra}),id);
  const publicRow = row => row && ({ id: row.id, number:field.number(row.id), metadata:row.field_metadata ? JSON.parse(row.field_metadata):null, device: row.device, created: row.created, status: row.status,
    attempts: row.attempts, result: row.result ? JSON.parse(row.result) : null, error: row.error,
    storage: c.driver, stored: Boolean(row.stored),
    extraction: row.extraction ? JSON.parse(row.extraction) : null,
    started:row.started, progress:row.progress ? JSON.parse(row.progress) : null,
    leaves: row.leaves ? JSON.parse(row.leaves).map(({object_key,...leaf}) => leaf) : [] });
  const get = id => publicRow(db.prepare('SELECT * FROM captures WHERE id=?').get(id));
  let active = false;
  return {
    get,field,
    list() { return db.prepare('SELECT * FROM captures ORDER BY created DESC LIMIT 100').all().map(publicRow); },
    async submit(bytes, device, idempotency, maskBytes, metadata={}) {
      const receivedAt=new Date().toISOString();
      if (typeof device !== 'string' || !/^[a-zA-Z0-9_. -]{1,80}$/.test(device)) throw Object.assign(new Error('Device must be 1–80 letters, digits, spaces, dots, underscores or hyphens'), { status: 400 });
      if (idempotency && !/^[a-zA-Z0-9_-]{8,128}$/.test(idempotency)) throw Object.assign(new Error('Invalid Idempotency-Key'), { status: 400 });
      const fieldMetadata=field.metadata(device,metadata);
      const metadataConflict=row=>row.field_metadata && JSON.parse(row.field_metadata).signature!==fieldMetadata.signature;
      let normalized, sourceSize;
      try {
        const image = sharp(bytes, { limitInputPixels: 40000000, failOn: 'warning' });
        const meta = await image.metadata();
        sourceSize = meta.orientation >= 5 && meta.orientation <= 8 ? [meta.height,meta.width] : [meta.width,meta.height];
        if (!['jpeg','png','webp'].includes(meta.format) || (meta.pages || 1) !== 1) throw new Error();
        normalized = await image.rotate().resize({ width: 2048, height: 2048, fit: 'inside', withoutEnlargement: true }).jpeg({ quality: 92 }).toBuffer();
      } catch { throw Object.assign(new Error('Upload a valid single JPEG, PNG or WebP image (maximum 40 megapixels)'), { status: 400 }); }
      const storedMeta = await sharp(normalized).metadata();
      const dimensions = {source_size:sourceSize,stored_size:[storedMeta.width,storedMeta.height]};
      const normalizedMask = maskBytes ? await normalizeMask(maskBytes,sourceSize,dimensions.stored_size) : null;
      const maskDigest = normalizedMask ? createHash('sha256').update(normalizedMask).digest('hex') : null;
      const digest = createHash('sha256').update(normalized).digest('hex');
      const existing = idempotency && db.prepare('SELECT * FROM captures WHERE idempotency=?').get(idempotency);
      if (existing) {
        if (existing.digest !== digest || existing.device !== device || existing.mask_digest !== maskDigest || metadataConflict(existing)) throw Object.assign(new Error('Idempotency key already used for a different capture'), { status: 409 });
        return publicRow(existing);
      }
      const id = randomUUID();
      const spool = path.join(c.dataDir, 'spool', `${id}.jpg`);
      await writeFile(spool, normalized, { flag: 'wx', flush: true });
      if (normalizedMask) await writeFile(path.join(c.dataDir,'spool',`${id}-mask.png`),normalizedMask,{flag:'wx',flush:true});
      // Check again after the async write to handle concurrent retries.
      const raced = idempotency && db.prepare('SELECT * FROM captures WHERE idempotency=?').get(idempotency);
      if (raced) { await unlink(spool); await unlink(path.join(c.dataDir,'spool',`${id}-mask.png`)).catch(()=>{}); if (raced.digest !== digest || raced.device !== device || raced.mask_digest !== maskDigest || metadataConflict(raced)) throw Object.assign(new Error('Idempotency conflict'), { status: 409 }); return publicRow(raced); }
      try {
        db.prepare('INSERT INTO captures (id,device,created,status,object_key,digest,idempotency,mask_key,mask_digest,dimensions,field_metadata) VALUES (?,?,?,?,?,?,?,?,?,?,?)')
          .run(id, device, receivedAt, 'queued', `captures/${id}.jpg`, digest, idempotency || null, normalizedMask ? `captures/${id}/ground-mask.png` : null,maskDigest,JSON.stringify(dimensions),JSON.stringify(fieldMetadata));
      } catch (error) { await unlink(spool); await unlink(path.join(c.dataDir,'spool',`${id}-mask.png`)).catch(()=>{}); throw error; }
      return get(id);
    },
    retry(id) {
      const row = get(id);
      if (!row) return null;
      if (!['failed','awaiting_model','awaiting_mask','no_candidates','retake_blurry'].includes(row.status)) throw Object.assign(new Error('Only failed, waiting or no-candidate captures can be retried'), { status: 409 });
      if (row.status === 'no_candidates') db.prepare('UPDATE captures SET extraction=NULL,leaves=NULL,result=NULL WHERE id=?').run(id);
      db.prepare("UPDATE captures SET status='queued', attempts=0, next_attempt=0, error=NULL WHERE id=?").run(id);
      progress(id,'queued','Waiting for the server worker');
      return get(id);
    },
    async attachMask(id, bytes) {
      const row = db.prepare('SELECT * FROM captures WHERE id=?').get(id);
      if (!row) return null;
      const allowed = ['awaiting_mask','no_candidates','failed','awaiting_model'];
      if (!allowed.includes(row.status)) throw Object.assign(new Error('Wait for extraction to finish before replacing its ground mask'),{status:409});
      const dimensions = row.dimensions ? JSON.parse(row.dimensions) : null;
      const meta = dimensions ? null : await sharp(await objects.get(row.object_key)).metadata();
      const normalized = await normalizeMask(bytes,dimensions?.source_size || [meta.width,meta.height],dimensions?.stored_size || [meta.width,meta.height]);
      const key = `captures/${id}/ground-mask.png`;
      await objects.put(key,normalized,'image/png');
      db.prepare("UPDATE captures SET mask_key=?,mask_digest=?,mask_stored=1,extraction=NULL,leaves=NULL,result=NULL,status='queued',attempts=0,next_attempt=0,error=NULL WHERE id=?")
        .run(key,createHash('sha256').update(normalized).digest('hex'),id);
      return get(id);
    },
    async tick() {
      if (active) return;
      field.purge();
      const row = db.prepare("SELECT * FROM captures WHERE status='queued' AND next_attempt<=? ORDER BY created LIMIT 1").get(now());
      if (!row) return;
      active = true;
      try {
        db.prepare('UPDATE captures SET started=COALESCE(started,?) WHERE id=?').run(new Date().toISOString(),row.id);
        progress(row.id,'storage','Saving the image to private storage');
        db.prepare("UPDATE captures SET status='uploading', attempts=attempts+1, error=NULL WHERE id=?").run(row.id);
        if (!row.stored) {
          await objects.put(row.object_key, await readFile(path.join(c.dataDir, 'spool', `${row.id}.jpg`)));
          db.prepare('UPDATE captures SET stored=1 WHERE id=?').run(row.id);
          await unlink(path.join(c.dataDir, 'spool', `${row.id}.jpg`)).catch(() => {});
        }
        if (row.mask_key && !row.mask_stored) {
          await objects.put(row.mask_key,await readFile(path.join(c.dataDir,'spool',`${row.id}-mask.png`)),'image/png');
          db.prepare('UPDATE captures SET mask_stored=1 WHERE id=?').run(row.id);
          await unlink(path.join(c.dataDir,'spool',`${row.id}-mask.png`)).catch(()=>{});
        }
        if (c.leafMode === 'heuristic') {
          let leaves = row.leaves ? JSON.parse(row.leaves) : null;
          if (!row.extraction) {
            if (c.groundMode === 'provided' && !row.mask_key) {
              progress(row.id,'awaiting_mask','Waiting for an aligned SegFormer-B2 ground mask');
              db.prepare("UPDATE captures SET status='awaiting_mask' WHERE id=?").run(row.id);
              return;
            }
            db.prepare("UPDATE captures SET status='splitting' WHERE id=?").run(row.id);
            progress(row.id,'ground_mask','Preparing ground mask and reading stored source image');
            const source = await objects.get(row.object_key);
            const mask = row.mask_key ? await objects.get(row.mask_key) : undefined;
            const {candidates,ground_mask,...extraction} = await split(source,mask,event=>{
              if (['ground_mask','leaf_separation'].includes(event.stage)) progress(row.id,event.stage,event.message);
            });
            progress(row.id,'saving_crops',`Saving ${candidates.length} leaf candidate crops`,{total_leaves:candidates.length,completed_leaves:0});
            const maskKey = row.mask_key || `captures/${row.id}/ground-mask.png`;
            if (!row.mask_key) await objects.put(maskKey,ground_mask,'image/png');
            leaves = [];
            for (const [index,candidate] of candidates.entries()) {
              const {bytes,...metadata} = candidate;
              const id = `${row.id}-leaf-${String(index+1).padStart(2,'0')}`;
              const key = `captures/${row.id}/${id}.jpg`;
              await objects.put(key,bytes);
              leaves.push({...metadata,id,index:index+1,object_key:key,status:'awaiting_model',result:null});
            }
            // Persist the manifest only after every crop has reached storage.
            db.prepare('UPDATE captures SET extraction=?, leaves=?, mask_key=?, mask_stored=1 WHERE id=?')
              .run(JSON.stringify(extraction),JSON.stringify(leaves),maskKey,row.id);
          }
          if (!leaves.length) {
            progress(row.id,'no_candidates','No usable leaf candidates; no disease verdict assigned');
            const extraction=JSON.parse(db.prepare('SELECT extraction FROM captures WHERE id=?').get(row.id).extraction);
            const status=extraction.status==='retake_blurry'?'retake_blurry':'no_candidates';
            db.prepare('UPDATE captures SET status=?,result=NULL WHERE id=?').run(status,row.id);
            if(status==='retake_blurry') progress(row.id,status,'Photo is blurry; retake with a steady camera and more light');
            return;
          }
          if (c.modelMode === 'disabled') {
            progress(row.id,'awaiting_model',`${leaves.length} leaf candidates saved; waiting for a disease model`,{total_leaves:leaves.length,completed_leaves:0});
            db.prepare("UPDATE captures SET status='awaiting_model' WHERE id=?").run(row.id);
            return;
          }
          db.prepare("UPDATE captures SET status='processing' WHERE id=?").run(row.id);
          for (const leaf of leaves) {
            if (leaf.result) continue;
            progress(row.id,'classifying',`Analyzing candidate ${leaf.index} of ${leaves.length}`,{current_leaf:leaf.index,total_leaves:leaves.length,completed_leaves:leaves.filter(item=>item.result).length});
            leaf.status='processing';
            db.prepare('UPDATE captures SET leaves=? WHERE id=?').run(JSON.stringify(leaves),row.id);
            leaf.result = await predict(await objects.get(leaf.object_key),leaf.id);
            leaf.status = 'complete';
            db.prepare('UPDATE captures SET leaves=? WHERE id=?').run(JSON.stringify(leaves),row.id);
          }
          const diseased = leaves.filter(leaf => leaf.result.verdict === 'diseased').length;
          const result = {verdict:diseased ? 'diseased' : 'healthy',simulated:leaves.some(leaf=>leaf.result.simulated),
            scope:'leaf_candidates',leaf_count:leaves.length,diseased_count:diseased,healthy_count:leaves.length-diseased};
          await field.imageEvent(row,result,()=>objects.get(row.object_key));
          db.prepare("UPDATE captures SET status='complete',result=? WHERE id=?").run(JSON.stringify(result),row.id);
          progress(row.id,'complete',`Finished ${leaves.length} leaf candidates`,{total_leaves:leaves.length,completed_leaves:leaves.length});
          return;
        }
        if (c.modelMode === 'disabled') {
          progress(row.id,'awaiting_model','Image saved; waiting for a model');
          db.prepare("UPDATE captures SET status='awaiting_model' WHERE id=?").run(row.id);
          return;
        }
        db.prepare("UPDATE captures SET status='processing' WHERE id=?").run(row.id);
        progress(row.id,'classifying','Running disease classification');
        // Deliberately read from B2, not the original upload/spool.
        const imageBytes=await objects.get(row.object_key);
        const result = await predict(imageBytes, row.id);
        await field.imageEvent(row,result,()=>imageBytes);
        db.prepare("UPDATE captures SET status='complete', result=? WHERE id=?").run(JSON.stringify(result), row.id);
        progress(row.id,'complete','Analysis complete');
      } catch (error) {
        if(error.code==='MODEL_UNAVAILABLE') {
          progress(row.id,'awaiting_model',error.message);
          db.prepare("UPDATE captures SET status='awaiting_model',error=? WHERE id=?").run(error.message,row.id);
          return;
        }
        const attempts = row.attempts + 1;
        progress(row.id,attempts>=3?'failed':'retrying',attempts>=3?'Processing failed after 3 attempts':'A processing step failed; retrying automatically');
        db.prepare('UPDATE captures SET status=?, error=?, next_attempt=? WHERE id=?').run(
          attempts >= 3 ? 'failed' : 'queued', 'Storage, ground-mask, leaf extraction or model service failed. Check server configuration and connectivity, then retry.', now() + 1000 * 2 ** attempts, row.id);
      } finally { active = false; }
    },
    async image(id) {
      const row = db.prepare('SELECT * FROM captures WHERE id=?').get(id);
      if (!row) return null;
      return row.stored ? objects.get(row.object_key) : readFile(path.join(c.dataDir, 'spool', `${id}.jpg`));
    },
    async leafImage(id, leafId) {
      const row = db.prepare('SELECT leaves FROM captures WHERE id=?').get(id);
      const leaf = row?.leaves && JSON.parse(row.leaves).find(leaf => leaf.id === leafId);
      return leaf ? objects.get(leaf.object_key) : null;
    },
    close() { c.gpuWorker?.child.kill(); db.close(); },
  };
}
