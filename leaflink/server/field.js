import { randomUUID } from 'node:crypto';

const defaults = { infected_samples:3, image_rule:'percent',infected_percent:10,cache_hours:24, cache_limit:100, grid_meters:5,
  origin_lat:0, origin_lon:0, voc_top_rise:10, voc_difference_rise:5,
  voc_hold:5, voc_max_gap_seconds:10, location_max_age_seconds:10 };
const fail = message => { throw Object.assign(new Error(message), {status:400}); };
export function location(value) {
  if (value == null) return null;
  if (typeof value !== 'object' || typeof value.lat !== 'number' || typeof value.lon !== 'number' ||
    !Number.isFinite(value.lat) || !Number.isFinite(value.lon) || Math.abs(value.lat)>85 || Math.abs(value.lon)>180) fail('Location needs numeric lat (-85 to 85) and lon (-180 to 180)');
  return {lat:value.lat,lon:value.lon};
}
export function timestamp(value, fallback = new Date().toISOString()) {
  if (value == null) return fallback;
  if (typeof value !== 'string' || !/^\d{4}-\d\d-\d\dT.*(?:Z|[+-]\d\d:\d\d)$/.test(value) || !Number.isFinite(Date.parse(value)) || Date.parse(value)>Date.now()+60000) fail('Use an ISO timestamp with timezone, no more than 60 seconds in the future');
  return new Date(value).toISOString();
}
function deviceName(value) {
  if (typeof value !== 'string' || !/^[a-zA-Z0-9_. -]{1,80}$/.test(value)) fail('Device must be 1–80 letters, digits, spaces, dots, underscores or hyphens');
  return value;
}
export function gridCell(loc,s) {
  if (!loc) return null;
  const rad=Math.PI/180, r=6371000;
  let delta=loc.lon-s.origin_lon;
  delta=((delta+540)%360)-180;
  return {x:Math.floor(r*delta*rad*Math.cos(s.origin_lat*rad)/s.grid_meters),
    y:Math.floor(r*(loc.lat-s.origin_lat)*rad/s.grid_meters)};
}
export function createFieldStore(db, now=Date.now) {
  db.exec(`CREATE TABLE IF NOT EXISTS field_settings (id INTEGER PRIMARY KEY CHECK(id=1), body TEXT NOT NULL);
    CREATE TABLE IF NOT EXISTS field_locations (device TEXT PRIMARY KEY, measured_at TEXT NOT NULL, received_at TEXT NOT NULL, body TEXT NOT NULL);
    CREATE TABLE IF NOT EXISTS field_location_samples (sequence INTEGER PRIMARY KEY AUTOINCREMENT, device TEXT NOT NULL, measured_at TEXT NOT NULL, received_at TEXT NOT NULL, body TEXT NOT NULL);
    CREATE TABLE IF NOT EXISTS field_samples (sequence INTEGER PRIMARY KEY AUTOINCREMENT, id TEXT NOT NULL, device TEXT NOT NULL, measured_at TEXT NOT NULL, received_at TEXT NOT NULL, body TEXT NOT NULL, UNIQUE(device,id));
    CREATE TABLE IF NOT EXISTS field_sensor_state (device TEXT PRIMARY KEY, body TEXT NOT NULL);
    CREATE TABLE IF NOT EXISTS field_events (id TEXT PRIMARY KEY, kind TEXT NOT NULL, capture_id TEXT UNIQUE, created TEXT NOT NULL, body TEXT NOT NULL);
    CREATE TABLE IF NOT EXISTS field_cache (event_id TEXT PRIMARY KEY, expires INTEGER NOT NULL, image BLOB NOT NULL);
    CREATE INDEX IF NOT EXISTS field_event_created ON field_events(created);
    CREATE INDEX IF NOT EXISTS field_sample_device ON field_samples(device,sequence);
    CREATE INDEX IF NOT EXISTS field_location_time ON field_location_samples(device,measured_at);
    CREATE TABLE IF NOT EXISTS capture_sequence (number INTEGER PRIMARY KEY AUTOINCREMENT, capture_id TEXT UNIQUE NOT NULL);`);
  db.prepare('INSERT OR IGNORE INTO field_settings VALUES (1,?)').run(JSON.stringify(defaults));
  db.exec('INSERT INTO capture_sequence(capture_id) SELECT id FROM captures WHERE id NOT IN (SELECT capture_id FROM capture_sequence) ORDER BY rowid');
  const settings=()=>{const saved=JSON.parse(db.prepare('SELECT body FROM field_settings WHERE id=1').get().body);return {...defaults,...saved,image_rule:saved.image_rule || 'count'};};
  const purge=()=>{
    db.prepare('DELETE FROM field_cache WHERE expires<=?').run(now());
    db.prepare('DELETE FROM field_cache WHERE event_id NOT IN (SELECT event_id FROM field_cache ORDER BY expires DESC, rowid DESC LIMIT ?)').run(settings().cache_limit);
  };
  const publicEvent=row=>{
    const cache=db.prepare('SELECT expires FROM field_cache WHERE event_id=? AND expires>?').get(row.id,now());
    return {...JSON.parse(row.body),id:row.id,kind:row.kind,created:row.created,capture_id:row.capture_id,
      cache_expires:cache ? new Date(cache.expires).toISOString():null};
  };
  const insertEvent=(kind,body,captureId=null)=>{
    const id=randomUUID(),created=new Date(now()).toISOString();
    db.prepare('INSERT INTO field_events VALUES (?,?,?,?,?)').run(id,kind,captureId,created,JSON.stringify(body));
    return id;
  };
  const latestLocation=(device,measuredAt)=>{
    const row=db.prepare('SELECT measured_at,body FROM field_location_samples WHERE device=? AND measured_at<=? ORDER BY measured_at DESC,sequence DESC LIMIT 1').get(device,measuredAt);
    const age=row ? Date.parse(measuredAt)-Date.parse(row.measured_at):Infinity;
    return age>=0 && age<=settings().location_max_age_seconds*1000 ? JSON.parse(row.body):null;
  };
  return {
    settings,purge,
    updateSettings(patch) {
      if (!patch || Array.isArray(patch) || typeof patch!=='object') fail('Settings must be an object');
      const s={...settings()};
      for (const [key,value] of Object.entries(patch)) {
        if(key==='image_rule'){if(!['count','percent'].includes(value))fail('image_rule must be count or percent');s[key]=value;continue;}
        if (!Object.hasOwn(defaults,key) || typeof value!=='number' || !Number.isFinite(value)) fail(`Invalid setting: ${key}`);
        s[key]=value;
      }
      // Preserve callers of the original count-only API.
      if(Object.hasOwn(patch,'infected_samples')&&!Object.hasOwn(patch,'image_rule')&&!Object.hasOwn(patch,'infected_percent'))s.image_rule='count';
      const bounds={infected_samples:[1,64],infected_percent:[1,100],cache_hours:[.01,168],cache_limit:[1,500],grid_meters:[1,1000],origin_lat:[-85,85],origin_lon:[-180,180],voc_top_rise:[.000001,1e9],voc_difference_rise:[.000001,1e9],voc_hold:[1,1000],voc_max_gap_seconds:[.1,3600],location_max_age_seconds:[0,3600]};
      for (const [key,[min,max]] of Object.entries(bounds)) if(s[key]<min||s[key]>max) fail(`${key} must be ${min}–${max}`);
      for (const key of ['infected_samples','cache_limit','voc_hold']) if(!Number.isInteger(s[key])) fail(`${key} must be an integer`);
      db.exec('BEGIN IMMEDIATE');
      try {
        const previous=settings();
        if(Object.keys(s).some(key=>key.startsWith('voc_')&&s[key]!==previous[key])) db.exec('DELETE FROM field_sensor_state');
        db.prepare('UPDATE field_settings SET body=? WHERE id=1').run(JSON.stringify(s));
        db.exec('COMMIT');
      } catch(error) {db.exec('ROLLBACK');throw error;}
      purge();return s;
    },
    metadata(device,input={}) {
      if (!input || typeof input!=='object' || Array.isArray(input)) fail('Capture metadata must be an object');
      const captured_at=timestamp(input.captured_at);
      const loc=location(input.location) || latestLocation(device,captured_at);
      return {captured_at,location:loc,signature:JSON.stringify({captured_at:input.captured_at || null,location:location(input.location)}),location_source:input.location?'capture':loc?'device_stream':null,
        policy:{image_rule:settings().image_rule,infected_percent:settings().infected_percent,infected_samples:settings().infected_samples,cache_hours:settings().cache_hours}};
    },
    number(id) {const old=db.prepare('SELECT number FROM capture_sequence WHERE capture_id=?').get(id);if(old)return old.number;return Number(db.prepare('INSERT INTO capture_sequence(capture_id) VALUES (?)').run(id).lastInsertRowid);},
    async imageEvent(row,result,loadBytes) {
      if(db.prepare('SELECT id FROM field_events WHERE capture_id=?').get(row.id)) return;
      const meta=JSON.parse(row.field_metadata || '{}'), policy=meta.policy || settings();
      const infected=result.diseased_count ?? (result.verdict==='diseased'?1:0);
      const leafCount=result.leaf_count ?? 1,percent=leafCount>0?100*infected/leafCount:0;
      if(!infected || (policy.image_rule==='percent'?percent<policy.infected_percent:infected<policy.infected_samples)) return;
      const bytes=await loadBytes();
      db.exec('BEGIN IMMEDIATE');
      try {
        const id=insertEvent('image',{device:row.device,location:meta.location || null,captured_at:meta.captured_at || row.created,
          infected_samples:infected,leaf_count:leafCount,infected_percent:percent,image_rule:policy.image_rule || 'count',
          threshold:policy.image_rule==='percent'?policy.infected_percent:policy.infected_samples,simulated:!!result.simulated},row.id);
        db.prepare('INSERT INTO field_cache VALUES (?,?,?)').run(id,now()+policy.cache_hours*3600000,bytes);
        db.exec('COMMIT');
      } catch(error) {db.exec('ROLLBACK');throw error;}
      purge();
    },
    ingestLocation(input) {
      const device=deviceName(input?.device), measured_at=timestamp(input.measured_at),loc=location(input.location);
      if(!loc) fail('Location is required');
      const old=db.prepare('SELECT measured_at FROM field_locations WHERE device=?').get(device);
      if(old && measured_at===old.measured_at) {
        const row=db.prepare('SELECT * FROM field_locations WHERE device=?').get(device);
        if(row.body===JSON.stringify(loc)) return {device,measured_at,received_at:row.received_at,location:loc,duplicate:true};
      }
      if(old && measured_at<=old.measured_at) fail('Location timestamps must increase for each device');
      const received_at=new Date(now()).toISOString();
      db.exec('BEGIN IMMEDIATE');
      try {
        db.prepare('INSERT OR REPLACE INTO field_locations VALUES (?,?,?,?)').run(device,measured_at,received_at,JSON.stringify(loc));
        db.prepare('INSERT INTO field_location_samples(device,measured_at,received_at,body) VALUES (?,?,?,?)').run(device,measured_at,received_at,JSON.stringify(loc));
        db.exec('DELETE FROM field_location_samples WHERE sequence < (SELECT COALESCE(MAX(sequence),0)-49999 FROM field_location_samples)');
        db.exec('COMMIT');
      } catch(error) {db.exec('ROLLBACK');throw error;}
      return {device,measured_at,received_at,location:loc};
    },
    ingest(input) {
      const batch=Array.isArray(input)?input:[input];
      if(!batch.length || batch.length>500) fail('Send 1–500 VOC samples per request');
      const results=[],s=settings();
      db.exec('BEGIN IMMEDIATE');
      try {
        for(const raw of batch) {
          if(!raw || typeof raw!=='object') fail('Each sample must be an object');
          const device=deviceName(raw.device),measured_at=timestamp(raw.measured_at),received_at=new Date(now()).toISOString();
          for(const key of ['top','bottom']) if(typeof raw[key]!=='number'||!Number.isFinite(raw[key])||Math.abs(raw[key])>1e12) fail(`${key} must be a finite numeric sensor reading`);
          if(typeof raw.units!=='string'||!raw.units.trim()||raw.units.length>40) fail('Specify comparable sensor units');
          if(raw.sample_id!=null && (typeof raw.sample_id!=='string'||!/^[\w-]{1,128}$/.test(raw.sample_id))) fail('Invalid sample_id');
          const id=raw.sample_id || randomUUID();
          const signature=JSON.stringify({top:raw.top,bottom:raw.bottom,units:raw.units,measured_at:raw.measured_at || null,location:location(raw.location)});
          const duplicate=db.prepare('SELECT body FROM field_samples WHERE device=? AND id=?').get(device,id);
          if(duplicate) {const old=JSON.parse(duplicate.body);if(old.signature!==signature) throw Object.assign(new Error('sample_id already used for different data'),{status:409});results.push({...old,duplicate:true});continue;}
          const saved=db.prepare('SELECT body FROM field_sensor_state WHERE device=?').get(device);
          let state=saved?JSON.parse(saved.body):null;
          const previous=db.prepare('SELECT measured_at FROM field_samples WHERE device=? ORDER BY sequence DESC LIMIT 1').get(device);
          if(previous && measured_at<=previous.measured_at) fail('VOC timestamps must increase for each device');
          const loc=location(raw.location)||latestLocation(device,measured_at),cell=gridCell(loc,s),cellKey=JSON.stringify(cell);
          if(state && state.units!==raw.units) fail('Sensor units changed; reset the baseline first');
          if(!state) state={top_reference:raw.top,bottom_reference:raw.bottom,count:0,units:raw.units};
          if(state.cell!==cellKey || Date.parse(measured_at)-Date.parse(state.measured_at)>s.voc_max_gap_seconds*1000) state.count=0;
          const dt=raw.top-state.top_reference,dbottom=raw.bottom-state.bottom_reference;
          const candidate=dt>s.voc_top_rise && dt-dbottom>s.voc_difference_rise;
          state.count=candidate?state.count+1:0;
          const sample={id,device,measured_at,received_at,top:raw.top,bottom:raw.bottom,units:raw.units,location:loc,
            top_change:dt,difference_change:dt-dbottom,consecutive:state.count,candidate,
            baseline:{top:state.top_reference,bottom:state.bottom_reference},signature};
          if(state.count===s.voc_hold) sample.event_id=insertEvent('voc',{device,location:loc,measured_at,
            top_change:dt,difference_change:dt-dbottom,measurements:state.count,units:raw.units,baseline:sample.baseline,simulated:false});
          state={...state,measured_at,cell:cellKey};
          db.prepare('INSERT OR REPLACE INTO field_sensor_state VALUES (?,?)').run(device,JSON.stringify(state));
          db.prepare('INSERT INTO field_samples(id,device,measured_at,received_at,body) VALUES (?,?,?,?,?)').run(id,device,measured_at,received_at,JSON.stringify(sample));
          results.push(sample);
        }
        // Retain the latest 50,000 raw samples; events and reference states persist.
        db.exec('DELETE FROM field_samples WHERE sequence < (SELECT COALESCE(MAX(sequence),0)-49999 FROM field_samples)');
        db.exec('COMMIT');return results;
      } catch(error) {db.exec('ROLLBACK');throw error;}
    },
    resetBaseline(device) {deviceName(device);db.prepare('DELETE FROM field_sensor_state WHERE device=?').run(device);return {device,reset:true};},
    events() {purge();return db.prepare('SELECT * FROM field_events ORDER BY created DESC,rowid DESC LIMIT 500').all().map(publicEvent);},
    eventImage(id) {purge();return db.prepare('SELECT image FROM field_cache WHERE event_id=?').get(id)?.image || null;},
    overview() {
      purge();const s=settings(),cells=new Map();
      for(const row of db.prepare('SELECT kind,body FROM field_events').iterate()) {
        const e=JSON.parse(row.body),cell=gridCell(e.location,s);if(!cell) continue;
        const key=`${cell.x},${cell.y}`;
        const count=cells.get(key)||{...cell,image:0,voc:0,simulated:0,simulated_image:0,simulated_voc:0,latest_capture_id:null};
        if(e.simulated) count.simulated++;else count[row.kind]++;
        if(e.simulated)count[`simulated_${row.kind}`]++;
        cells.set(key,count);
      }
      // Point navigation remains possible even for cells outside the latest 500-event feed.
      for(const row of db.prepare("SELECT capture_id,body FROM field_events WHERE kind='image' ORDER BY created,rowid").iterate()){
        const e=JSON.parse(row.body),cell=gridCell(e.location,s);if(!cell)continue;
        cells.get(`${cell.x},${cell.y}`).latest_capture_id=row.capture_id;
      }
      return {settings:s,cells:[...cells.values()],total_events:db.prepare('SELECT COUNT(*) AS n FROM field_events').get().n,
        unmapped:db.prepare("SELECT COUNT(*) AS n FROM field_events WHERE json_extract(body,'$.location') IS NULL").get().n,
        cache_count:db.prepare('SELECT COUNT(*) AS n FROM field_cache').get().n,
        locations:db.prepare('SELECT * FROM field_locations ORDER BY received_at DESC LIMIT 100').all().map(r=>({...r,location:JSON.parse(r.body),body:undefined})),
        location_history:db.prepare('SELECT device,measured_at,received_at,body FROM field_location_samples ORDER BY sequence DESC LIMIT 100').all().map(r=>({...r,location:JSON.parse(r.body),body:undefined})),
        samples:db.prepare('SELECT body FROM field_samples ORDER BY sequence DESC LIMIT 100').all().map(r=>{const {signature,...sample}=JSON.parse(r.body);return sample;})};
    }
  };
}
