import test from 'node:test';
import assert from 'node:assert/strict';
import {groupMarkers,project,trails} from '../app/map-data.js';
test('map shows both modalities including simulation without treating simulation as a real event',()=>{
  const [m]=groupMarkers([{x:1,y:2,image:0,voc:1,simulated:1,simulated_image:1,simulated_voc:0,latest_capture_id:'photo-1'}]);
  assert.equal(m.kind,'both');assert.equal(m.image,0);assert.equal(m.simulated,1);assert.equal(m.latest_capture_id,'photo-1');
});
test('zoom grouping preserves photo navigation and true observation center',()=>{
  const cells=[{x:1,y:1,image:1,voc:0,latest_capture_id:'one'},{x:2,y:1,image:0,voc:1}];
  const wide=groupMarkers(cells,4);assert.equal(wide.length,1);assert.equal(wide[0].kind,'both');assert.equal(wide[0].world_x,2);
  const zoomed=groupMarkers(cells,.25);assert.equal(zoomed[0].world_x,1.5);assert.equal(zoomed[0].latest_capture_id,'one');
});
test('GPS trails remain device-scoped and chronological; null locations stay unmapped',()=>{
  const s={origin_lat:0,origin_lon:0,grid_meters:5};assert.equal(project(null,s),null);
  const result=trails([{device:'a',measured_at:'2026-01-01T00:00:02Z',location:{lat:.001,lon:.001}},{device:'b',measured_at:'2026-01-01T00:00:01Z',location:{lat:.002,lon:.002}},{device:'a',measured_at:'2026-01-01T00:00:00Z',location:{lat:0,lon:0}}],s);
  const a=result.find(r=>r.device==='a');assert.equal(a.points.length,2);assert.equal(a.points[0].x,0);assert.ok(a.points[1].x>0);
});
