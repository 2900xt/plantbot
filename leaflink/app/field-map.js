import {project,groupMarkers,trails} from './map-data.js';
const $=id=>document.getElementById(id);
const ink='#263d5b',colors={image:'#23759a',voc:'#ad5e18',both:'#44745a',simulation:'#85638c'};
const escape=value=>String(value??'').replace(/[&<>"']/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
// Simulated rover: starts at the grid origin and drives forward (north) at 1 ft/s, nonstop.
// The start time survives reloads within this tab so the run keeps its distance.
const ROVER_MPS=0.3048,ROVER_START_KEY='leaflink-rover-sim-start';
const roverStart=(()=>{let t=0;try{t=Number(sessionStorage.getItem(ROVER_START_KEY));}catch{}if(!Number.isFinite(t)||t<=0||t>Date.now()){t=Date.now();try{sessionStorage.setItem(ROVER_START_KEY,String(t));}catch{}}return t;})();
const roverMeters=()=>ROVER_MPS*(Date.now()-roverStart)/1000;
const ROVER_ICON='<rect class="rover-wheel" x="-16" y="-13" width="6" height="10" rx="2"/><rect class="rover-wheel" x="10" y="-13" width="6" height="10" rx="2"/><rect class="rover-wheel" x="-16" y="5" width="6" height="10" rx="2"/><rect class="rover-wheel" x="10" y="5" width="6" height="10" rx="2"/><rect class="rover-body" x="-11" y="-16" width="22" height="32" rx="6"/><circle class="rover-eye" cx="0" cy="-8" r="3.5"/><path class="rover-nose" d="M-6 6 0 0 6 6"/>';

export function createFieldMap({onSelect,onOpen,onNotice,onPreview=()=>{},onPreviewLeave=()=>{},onMapChange=()=>{}}){
  let field=null,captures=[],settings=null,signal='all',center={x:0,y:0},stride=1,initialized=false,last='',drag=null;
  let view={width:900,height:610,unit:64},follow=true;
  const canvas=$('grid');
  const redraw=()=>render(field,captures,settings,signal);
  const roverWorld=()=>({x:0,y:settings?roverMeters()/settings.grid_meters:0});
  function fit(){
    if(!settings)return;
    follow=true;
    const points=[...(field?.cells||[]).map(c=>({x:c.x+.5,y:c.y+.5})),...(field?.location_history||[]).map(p=>project(p.location,settings)),...captures.map(c=>project(c.metadata?.location,settings)),{x:0,y:0},roverWorld()].filter(Boolean);
    if(!points.length){center={x:0,y:0};stride=1;}else{
      const xs=points.map(p=>p.x),ys=points.map(p=>p.y),loX=Math.min(...xs),hiX=Math.max(...xs),loY=Math.min(...ys),hiY=Math.max(...ys);
      center={x:(loX+hiX)/2,y:(loY+hiY)/2};
      stride=Math.max(1,Math.ceil(Math.max((hiX-loX+2)/(innerWidth<=760?5:13),(hiY-loY+2)/(innerWidth<=760?7:9))));
    }
    initialized=true;redraw();
  }
  function render(nextField,nextCaptures,nextSettings,nextSignal='all'){
    field=nextField;captures=nextCaptures;settings=nextSettings;signal=nextSignal;
    if(!initialized&&settings&&((field?.cells.length||0)||(field?.location_history.length||0)||captures.some(c=>c.metadata?.location))){fit();return;}
    const mobile=innerWidth<=760,width=mobile?400:900,height=mobile?470:610,unit=mobile?60:64;
    view={width,height,unit};
    canvas.setAttribute('viewBox',`0 0 ${width} ${height}`);
    const xy=p=>({x:width/2+(p.x-center.x)*unit/stride,y:height/2-(p.y-center.y)*unit/stride});
    const viewPoints=[];let markup=`<defs><clipPath id="map-clip"><rect x="0" y="0" width="${width}" height="${height}"/></clipPath></defs><g clip-path="url(#map-clip)">`;
    for(let x=((width/2-center.x*unit/stride)%unit+unit)%unit;x<width;x+=unit)markup+=`<path class="map-grid" d="M${x},0V${height}"/>`;
    for(let y=((height/2+center.y*unit/stride)%unit+unit)%unit;y<height;y+=unit)markup+=`<path class="map-grid" d="M0,${y}H${width}"/>`;
    if(settings){
      for(const trail of trails(field?.location_history||[],settings)){
        // A line joins only measurements from the same device. It is a recorded GPS trail.
        const paths=[];let segment=[];
        for(const point of trail.points){
          if(segment.length&&Date.parse(point.at)-Date.parse(segment.at(-1).at)>settings.voc_max_gap_seconds*1000){paths.push(segment);segment=[];}
          segment.push(point);
        }
        paths.push(segment);
        for(const segment of paths){
          if(segment.length>1)markup+=`<polyline class="field-trail" points="${segment.map(p=>{const q=xy(p);return `${q.x},${q.y}`;}).join(' ')}"><title>${escape(trail.device)} GPS trail</title></polyline>`;
          for(const p of segment){const q=xy(p);markup+=`<circle class="trail-point" cx="${q.x}" cy="${q.y}" r="${mobile?6:7}"><title>${escape(p.device)} · ${escape(new Date(p.at).toLocaleTimeString())}</title></circle>`;}
        }
      }
      for(const c of captures){
        const p=project(c.metadata?.location,settings);if(!p)continue;const q=xy(p);
        if(q.x<0||q.y<0||q.x>width||q.y>height)continue;
        const key=`capture-${c.id}`;viewPoints.push({key,capture_id:c.id,capture:c});
        markup+=`<g role="button" tabindex="0" class="observation-point" data-key="${key}" transform="translate(${q.x},${q.y})" aria-label="Image ${c.number}. Open photo"><title>Image #${c.number}</title><circle class="hit-area" r="26"/><circle class="capture-point" r="10"/></g>`;
      }
      for(const marker of groupMarkers(field?.cells||[],stride)){
        if(signal!=='all'&&(signal==='both'?marker.kind!=='both':signal==='image'?!['image','both'].includes(marker.kind):!['voc','both'].includes(marker.kind)))continue;
        const q=xy({x:marker.world_x,y:marker.world_y});
        if(q.x<-25||q.y<-25||q.x>width+25||q.y>height+25)continue;
        const key=`cell-${marker.x},${marker.y}`,label=marker.kind==='both'?'CV and VOC anomalies':marker.kind==='image'?'CV anomaly':marker.kind==='voc'?'VOC anomaly':'Simulated anomaly';
        const annotation=marker.simulated?' · includes simulated data':'';
        viewPoints.push({key,marker,capture_id:marker.latest_capture_id});
        const shape=marker.kind==='both'?'<path class="skull-shape" d="M-14 7C-27 3-25-15-15-21C-7-28 10-28 18-18C28-5 22 6 13 9L12 21H-12Z"/><circle class="skull-eye" cx="-9" cy="-5" r="5"/><circle class="skull-eye" cx="9" cy="-5" r="5"/><path class="skull-nose" d="m0 2-4 6h8Z"/><path class="skull-teeth" d="M-7 14v7M0 14v7M7 14v7"/>':'<path class="marker-shape" d="M0-25 26 22H-26Z"/><path class="marker-face" d="M0-9v13M0 12v1"/>';
        markup+=`<g role="button" tabindex="0" class="anomaly-marker ${marker.kind}" data-key="${key}" transform="translate(${q.x},${q.y})" aria-label="${label}, ${marker.total} events${annotation}. ${marker.latest_capture_id?'Double-click to open image':'No linked photo'}"><title>${label}${annotation}</title><circle class="hit-area" r="30"/>${shape}${marker.simulated?'<circle class="simulation-mark" cx="21" cy="-20" r="6"/>':''}</g>`;
      }
      // Static shell only, so the markup cache still works; placeRover() moves it every frame.
      markup+=`<g class="rover-sim" aria-hidden="true"><path class="rover-trail" data-rover="trail"/><circle class="rover-start" data-rover="start" r="5"/><g data-rover="icon">${ROVER_ICON}</g><text class="rover-label" data-rover="label"></text></g>`;
    }
    markup+=`</g><text class="north" x="${width-28}" y="32" text-anchor="middle">N</text><path class="north-arrow" d="M${width-28},42v25m-5-18 5-7 5 7"/><path class="scale-bar" d="M28,${height-28}v-5h${unit}v5"/><text class="scale-text" x="28" y="${height-42}">${settings?Math.round(stride*settings.grid_meters):5} m</text>`;
    $('grid-caption').textContent=settings?`${settings.grid_meters} m field grid`:'Field map';
    $('map-note').textContent=field?`${field.total_events} anomalies · ${field.unmapped} without location`:'No observations yet';
    if(markup===last){placeRover();return;}
    onMapChange();
    const oldKey=canvas.contains(document.activeElement)?document.activeElement.dataset.key:null;
    last=markup;canvas.innerHTML=markup;
    for(const node of canvas.querySelectorAll('[data-key]')){
      const item=viewPoints.find(p=>p.key===node.dataset.key);
      node.onclick=()=>onSelect(item,stride,node);
      if(item.marker){
        let pointerActivation=false;
        node.onpointerdown=()=>{pointerActivation=true;};
        node.onpointerup=()=>queueMicrotask(()=>{pointerActivation=false;});
        node.onpointercancel=()=>{pointerActivation=false;};
        node.onpointerenter=e=>{if(e.pointerType!=='touch')onPreview(item,stride,node,'hover');};
        node.onpointerleave=()=>onPreviewLeave();
        node.onfocus=()=>{if(!pointerActivation)onPreview(item,stride,node,'focus');};
        node.onblur=()=>onPreviewLeave();
      }
      node.ondblclick=e=>{e.preventDefault();if(item.capture_id)onOpen(item.capture_id);else onNotice('This VOC anomaly has no linked photo.');};
      node.onkeydown=e=>{
        if(e.key==='Enter'||e.key===' '){e.preventDefault();if(e.key==='Enter'&&item.capture_id)onOpen(item.capture_id);else onSelect(item,stride,node);}
      };
      if(node.dataset.key===oldKey)node.focus({preventScroll:true});
    }
    placeRover();
  }
  function placeRover(){
    const icon=canvas.querySelector('[data-rover="icon"]');if(!icon||!settings)return;
    const {width,height,unit}=view,xy=p=>({x:width/2+(p.x-center.x)*unit/stride,y:height/2-(p.y-center.y)*unit/stride});
    const world=roverWorld(),q=xy(world),start=xy({x:0,y:0});
    // Keep the rover in view while following: once it nears the top edge (or leaves
    // sideways), recenter so it sits low in the frame with room ahead.
    if(follow&&!drag&&(q.y<height*.15||q.y>height||q.x<0||q.x>width)){
      center={x:0,y:world.y+height*.25*stride/unit};redraw();return;
    }
    icon.setAttribute('transform',`translate(${q.x.toFixed(1)},${q.y.toFixed(1)})`);
    canvas.querySelector('[data-rover="start"]').setAttribute('transform',`translate(${start.x.toFixed(1)},${start.y.toFixed(1)})`);
    canvas.querySelector('[data-rover="trail"]').setAttribute('d',`M${start.x.toFixed(1)},${start.y.toFixed(1)}L${q.x.toFixed(1)},${q.y.toFixed(1)}`);
    const label=canvas.querySelector('[data-rover="label"]'),feet=Math.floor(roverMeters()/.3048);
    label.setAttribute('x',(q.x+24).toFixed(1));label.setAttribute('y',(q.y+5).toFixed(1));
    label.textContent=`Rover · 1 ft/s · ${feet.toLocaleString()} ft`;
  }
  const tick=()=>{placeRover();requestAnimationFrame(tick);};
  requestAnimationFrame(tick);
  $('fit').onclick=fit;
  $('zoom-in').onclick=()=>{stride=Math.max(.25,stride/2);redraw();};
  $('zoom-out').onclick=()=>{stride=Math.min(1e7,stride*2);redraw();};
  // Panning by hand stops following the rover; Fit field turns it back on.
  $('fullscreen').onclick=async()=>{
    try{if(document.fullscreenElement)await document.exitFullscreen();else await $('field-map').requestFullscreen();}
    catch{onNotice('Fullscreen is unavailable in this browser.');}
  };
  document.addEventListener('fullscreenchange',()=>{$('fullscreen').setAttribute('aria-label',document.fullscreenElement?'Exit expanded map':'Expand field map');});
  const localPoint=e=>new DOMPoint(e.clientX,e.clientY).matrixTransform(canvas.getScreenCTM().inverse());
  canvas.addEventListener('pointerdown',e=>{if(e.target.closest('[role="button"]'))return;onMapChange();drag=localPoint(e);canvas.setPointerCapture(e.pointerId);});
  canvas.addEventListener('pointerup',e=>{
    if(!drag)return;const point=localPoint(e),unit=innerWidth<=760?60:64;
    if(Math.hypot(point.x-drag.x,point.y-drag.y)>3)follow=false;
    center.x-=(point.x-drag.x)*stride/unit;center.y+=(point.y-drag.y)*stride/unit;drag=null;redraw();
  });
  canvas.addEventListener('pointercancel',()=>{drag=null;});
  window.addEventListener('resize',redraw);
  const geometry=()=>settings&&[settings.origin_lat,settings.origin_lon,settings.grid_meters].join(',');
  return{render,fit,getStride:()=>stride,
    getState:()=>({center:{...center},stride,geometry:geometry()}),
    restoreState(state){
      if(!state||state.geometry!==geometry()||!Number.isFinite(state.center?.x)||!Number.isFinite(state.center?.y)||!Number.isFinite(state.stride)||state.stride<=0)return;
      center={...state.center};stride=state.stride;initialized=true;redraw();
    },
    reset(){initialized=false;last='';center={x:0,y:0};stride=1;}};
}
