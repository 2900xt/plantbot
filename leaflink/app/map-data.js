// Pure view transforms: marker identity reflects modalities, never a disease diagnosis.
export function project(location,settings){
  if(!location)return null;
  const rad=Math.PI/180,r=6371000,delta=((location.lon-settings.origin_lon+540)%360)-180;
  return{x:r*delta*rad*Math.cos(settings.origin_lat*rad)/settings.grid_meters,y:r*(location.lat-settings.origin_lat)*rad/settings.grid_meters};
}
export function markerKind(cell){
  const cv=cell.image+(cell.simulated_image||0),voc=cell.voc+(cell.simulated_voc||0);
  return cv&&voc?'both':cv?'image':voc?'voc':'simulation';
}
export function groupMarkers(cells,stride=1){
  const groups=new Map();
  for(const cell of cells){
    const x=Math.floor(cell.x/stride),y=Math.floor(cell.y/stride),key=`${x},${y}`;
    const g=groups.get(key)||{x,y,image:0,voc:0,simulated:0,simulated_image:0,simulated_voc:0,latest_capture_id:null,cells:0,world_x:0,world_y:0};
    for(const k of ['image','voc','simulated','simulated_image','simulated_voc'])g[k]+=cell[k]||0;
    g.cells++;g.world_x+=cell.x+.5;g.world_y+=cell.y+.5;if(cell.latest_capture_id)g.latest_capture_id=cell.latest_capture_id;
    groups.set(key,g);
  }
  return [...groups.values()].map(g=>({...g,world_x:g.world_x/g.cells,world_y:g.world_y/g.cells,kind:markerKind(g),total:g.image+g.voc+g.simulated}));
}
export function trails(history,settings){
  const devices=new Map();
  for(const row of [...history].sort((a,b)=>a.measured_at.localeCompare(b.measured_at))){
    if(!row.location)continue;
    const point={...project(row.location,settings),at:row.measured_at,device:row.device};
    const list=devices.get(row.device)||[];list.push(point);devices.set(row.device,list);
  }
  return [...devices.entries()].map(([device,points])=>({device,points}));
}
