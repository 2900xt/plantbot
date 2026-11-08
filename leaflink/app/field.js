import './field.css';
import {createFieldMap} from './field-map.js';
import {groupMarkers} from './map-data.js';
import './rover.js';
import {candidateSummary,scoreLabel,verdictLabel} from './verdict.js';
const $=id=>document.getElementById(id);
const esc=value=>String(value??'').replace(/[&<>"']/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
const date=value=>value?new Date(value).toLocaleString(): '—';
const num=value=>Number(value).toLocaleString(undefined,{maximumFractionDigits:3});
const percent=value=>Number(value).toLocaleString(undefined,{maximumFractionDigits:1});
let base=sessionStorage.getItem('field-url')||'',token=sessionStorage.getItem('field-token')||'';
let connected=false,busy=false,field=null,events=[],captures=[],selected=null,settings=null;
let filter=null,eventSignature='',detailSignature='',gpsId=null,gpsDevice=null,lastGps=0;
let generation=0,uploadKey=null,urls=new Map();
let lastCaptures='',signal='all',galleryGeneration=0,lastMapReturn=null,lastMapPointKey=null;
try{const saved=JSON.parse(sessionStorage.getItem('field-map-return')||'null');if(saved?.base===base)lastMapReturn=saved;}catch{}
let previewState=null,previewTimer=null,suppressFocusPreview=false;
const map=createFieldMap({onSelect:selectPoint,onOpen:openCapture,onNotice:message=>notice(message),onPreview:showHoverPreview,onPreviewLeave:schedulePreviewClose,onMapChange:hidePreview});
$('url').value=base;$('token').value=token;
// Loading bar under the header. Each job is {label, value 0-100 or null = indeterminate};
// the newest job is shown, and the bar fills to 100% and fades once every job is done.
const loadJobs=new Map();let loadHideTimer=null;
function loading(key,label,value){
  loadJobs.delete(key);if(value!==false)loadJobs.set(key,{label,value});
  const bar=$('load-bar'),fill=bar.firstElementChild,text=$('load-label'),job=[...loadJobs.values()].at(-1);
  clearTimeout(loadHideTimer);
  if(!job){
    if(bar.hidden)return;
    bar.classList.remove('indeterminate');fill.style.width='100%';bar.setAttribute('aria-valuenow','100');text.hidden=true;
    loadHideTimer=setTimeout(()=>{if(!loadJobs.size){bar.hidden=true;fill.style.width='0%';}},450);
    return;
  }
  bar.hidden=false;text.hidden=false;text.textContent=job.label;bar.setAttribute('aria-valuetext',job.label);
  bar.classList.toggle('indeterminate',job.value==null);
  if(job.value==null){fill.style.width='';bar.removeAttribute('aria-valuenow');}
  else{const pct=Math.max(0,Math.min(100,Math.round(job.value)));fill.style.width=pct+'%';bar.setAttribute('aria-valuenow',String(pct));}
}
// Pipeline progress for one capture, 0-100. Same six steps as the capture page:
// upload/store, ground mask, separate leaves, classify (per leaf), results.
const ACTIVE=['queued','uploading','splitting','processing'];
const STAGE_STEP={queued:1,storage:1,ground_mask:2,leaf_separation:3,saving_crops:4,classifying:4,complete:6,awaiting_mask:2,awaiting_model:4,no_candidates:4};
function pipelinePercent(c){
  if(c.status==='complete')return 100;
  const p=c.progress||{};let step=STAGE_STEP[p.stage]??(c.stored?2:1);
  if(p.stage==='classifying'&&p.total_leaves)step=4+(p.completed_leaves||0)/p.total_leaves;
  return Math.round(100*step/6);
}
function pipelineLabel(c){
  const p=c.progress||{};
  if(p.stage==='classifying'&&p.total_leaves)return `Analyzing leaf ${Math.min((p.completed_leaves||0)+1,p.total_leaves)} of ${p.total_leaves}`;
  return {ground_mask:'Finding ground',leaf_separation:'Separating leaves',saving_crops:'Saving leaf crops',classifying:'Analyzing leaves'}[p.stage]||(c.status==='queued'?'Queued':'Processing');
}
function pipelineBar(c,cls){
  if(!ACTIVE.includes(c.status))return '';
  const pct=pipelinePercent(c);
  return `<div class="${cls}"><div class="ll-progress" role="progressbar" aria-label="Analysis progress" aria-valuemin="0" aria-valuemax="100" aria-valuenow="${pct}" aria-valuetext="${esc(pipelineLabel(c))}, ${pct}%"><i data-pct="${pct}"></i></div><span>${esc(pipelineLabel(c))} · ${pct}%</span></div>`;
}
// The CSP forbids inline style attributes, so widths are applied through the CSSOM.
function applyBarWidths(root){for(const fill of root.querySelectorAll('.ll-progress>i[data-pct]'))fill.style.width=fill.dataset.pct+'%';}
function notice(text,error=false){$('notice').textContent=text;$('notice').classList.toggle('error',error);}
function toggleConnection(show){$('connection').hidden=!show;$('connection-toggle').setAttribute('aria-expanded',String(show));}
function tab(name){if(name!=='monitor')hidePreview();if(name!=='monitor'&&!$('monitor').hidden){lastMapReturn={base,map:map.getState(),scroll:window.scrollY,filter:filter?{...filter}:null,signal,key:lastMapPointKey,vocOpen:$('voc-panel').open,cvOpen:$('cv-panel').open};sessionStorage.setItem('field-map-return',JSON.stringify(lastMapReturn));}for(const node of document.querySelectorAll('.tab-panel'))node.hidden=node.id!==name;for(const node of document.querySelectorAll('[data-tab]')){node.classList.toggle('active',node.dataset.tab===name);node.setAttribute('aria-pressed',String(node.dataset.tab===name));} $('utility-menu').hidden=true; $('tools-toggle').setAttribute('aria-expanded','false'); if(name==='capture')location.hash=selected?'images/'+selected:'images';else location.hash=name==='monitor'?'field':name;}
for(const node of document.querySelectorAll('[data-tab]'))node.onclick=()=>tab(node.dataset.tab);
$('connection-toggle').onclick=()=>toggleConnection($('connection').hidden);
async function request(route,options={}){
  if(!token)throw new Error('Connect with your API token first');
  const response=await fetch(`${base}/api${route}`,{...options,headers:{Authorization:`Bearer ${token}`,...options.headers},signal:AbortSignal.timeout(30000)});
  if(!response.ok){const body=await response.json().catch(()=>({}));throw new Error(body.error||`Server returned ${response.status}`);}
  return response;
}
async function json(route,body,method='POST'){return(await request(route,{method,headers:{'Content-Type':'application/json'},body:JSON.stringify(body)})).json();}
async function image(route,key,node,{silent=false}={}){
  const current=generation;
  try{if(!urls.has(key)){const response=await request(route);const blob=await response.blob();if(current!==generation)return;urls.set(key,URL.createObjectURL(blob));}if(current===generation&&node.isConnected)node.src=urls.get(key);}
  catch(error){if(node.isConnected){node.alt=error.message;if(!silent)notice(error.message,true);}}
}
function stopGps(){if(gpsId!=null)navigator.geolocation.clearWatch(gpsId);gpsId=null;gpsDevice=null;$('gps').textContent='Start GPS stream';$('gps-status').textContent='GPS off';}
function clearData(){generation++;galleryGeneration++;loading('photos',null,false);stopGps();for(const url of urls.values())URL.revokeObjectURL(url);urls.clear();field=null;events=[];captures=[];selected=null;filter=null;eventSignature='';detailSignature='';lastCaptures='';map.reset();lastMapReturn=null;lastMapPointKey=null;sessionStorage.removeItem('field-map-return');$('captures').innerHTML='<tr><td colspan="6">No images loaded.</td></tr>';$('image-gallery').innerHTML='<p class="empty">Connect to load images.</p>';$('voc-events').innerHTML='<p class="empty">No VOC anomalies yet.</p>';$('cv-events').innerHTML='<p class="empty">No CV anomalies yet.</p>';$('voc-count').textContent='0';$('cv-count').textContent='0';$('marker-note').hidden=true;$('samples').innerHTML='<tr><td colspan="4">No measurements loaded.</td></tr>';$('capture-detail').innerHTML='<p class="empty">Select an image to see the full photo.</p>';$('locations').textContent='No positions loaded.';$('sensor-summary').textContent='No measurements loaded.';$('config').innerHTML='<dt>Connection</dt><dd>Not connected</dd>';$('capture-count').textContent='No images loaded';$('event-summary').textContent='No anomalies';$('last-sync').textContent='No server contact yet';$('mode-label').hidden=true;$('detection').disabled=true;renderGrid();}
$('disconnect').onclick=()=>{connected=false;token='';sessionStorage.removeItem('field-token');$('token').value='';$('status').textContent='Disconnected';$('status').classList.remove('online');clearData();toggleConnection(true);notice('Disconnected. Your token has been cleared from this tab.');};
$('connect-form').onsubmit=async event=>{
  event.preventDefault();const value=$('url').value.trim().replace(/\/+$/,'');
  if(value){try{const u=new URL(value);if(!['http:','https:'].includes(u.protocol)||u.username||u.password||u.pathname!=='/'||u.search||u.hash)throw new Error();}catch{return notice('Use a server origin such as https://leaf.example.com without a path.',true);}}
  connected=false;clearData();base=value;token=$('token').value.trim();sessionStorage.setItem('field-url',base);sessionStorage.setItem('field-token',token);
  try{await connect();}catch(error){notice(error.message,true);}
};
async function connect(){
  loading('connect','Connecting to server',8);
  try{
    const config=await(await request('/config')).json();
    loading('connect','Loading settings',35);
    connected=true;$('status').textContent='Connected';$('status').classList.add('online');toggleConnection(false);
    $('config').innerHTML=Object.entries({Storage:config.storage,Classifier:config.model,'Ground mask':config.ground_mask,'Leaf extraction':config.leaf_extraction}).map(([k,v])=>`<dt>${esc(k)}</dt><dd>${esc(v)}</dd>`).join('');
    $('mode-label').hidden=config.model!=='demo';
    notice(config.model==='demo'?'Simulation mode':config.model==='disabled'?'Classifier disabled. Images can be separated but will wait for a model.':'Field connected');
    settings=await(await request('/settings')).json();fillRules();
    loading('connect','Loading field data',65);
    await refresh();applyRoute();
  }finally{loading('connect',null,false);}
}
function fillRules(){for(const [key,value] of Object.entries(settings||{})){const input=$('rules-form').elements.namedItem(key);if(input)input.value=value;}syncDetection();}
async function refresh({manual=false}={}){
  if(busy||!connected)return;busy=true;const current=generation;
  if(manual)loading('refresh','Refreshing field',null);
  try{
    const results=await Promise.all(['/field','/events','/captures'].map(async route=>(await request(route)).json()));
    if(current!==generation)return;
    [field,events,captures]=results;settings=field.settings;
    $('status').textContent='Connected';$('status').classList.add('online');$('last-sync').textContent=`Last sync ${new Date().toLocaleTimeString()}`;
    renderGrid();renderEvents();renderCaptures();renderSensors();renderGas();renderDrift();
    if(selected)await renderDetail();
  }catch(error){if(current===generation){$('status').textContent='Connection lost';$('status').classList.remove('online');notice(`${error.message}. Data shown may be stale; retry or reconnect.`,true);}}
  finally{busy=false;if(manual)loading('refresh',null,false);}
}
$('refresh').onclick=()=>refresh({manual:true});
function cell(loc){if(!loc||!settings)return null;const rad=Math.PI/180,r=6371000;const d=((loc.lon-settings.origin_lon+540)%360)-180;return{x:Math.floor(r*d*rad*Math.cos(settings.origin_lat*rad)/settings.grid_meters),y:Math.floor(r*(loc.lat-settings.origin_lat)*rad/settings.grid_meters)};}
function renderGrid(){map.render(field,captures,settings,signal);}
$('clear-filter').onclick=()=>{filter=null;eventSignature='';hidePreview();renderEvents();};
function cancelPreviewClose(){if(previewTimer)clearTimeout(previewTimer);previewTimer=null;}
function hidePreview(){
  cancelPreviewClose();
  if(previewState?.anchor)previewState.anchor.removeAttribute('aria-describedby');
  previewState=null;$('marker-note').hidden=true;
}
  function dismissPreview(){
    const anchor=previewState?.anchor;
    hidePreview();suppressFocusPreview=true;
    anchor?.focus({preventScroll:true});
    queueMicrotask(()=>{suppressFocusPreview=false;});
  }
function schedulePreviewClose(){
  cancelPreviewClose();
  previewTimer=setTimeout(()=>{
    const note=$('marker-note'),anchor=previewState?.anchor;
    if(previewState?.pinned||note.matches(':hover')||note.contains(document.activeElement)||anchor===document.activeElement||anchor?.matches(':hover'))return;
    hidePreview();
  },220);
}
function placePreview(anchor){
  const note=$('marker-note'),wrap=note.parentElement,box=wrap.getBoundingClientRect(),point=anchor.getBoundingClientRect();
  const width=note.offsetWidth,height=note.offsetHeight,gap=12,pad=12;
  const right=point.right-box.left+gap,left=point.left-box.left-width-gap;
  note.style.left=Math.max(pad,Math.min(right+width<=box.width-pad?right:left,box.width-width-pad))+'px';
  note.style.top=Math.max(pad,Math.min(point.top-box.top+point.height/2-height/2,box.height-height-pad))+'px';
}
function showHoverPreview(item,stride,anchor,source){
  if(source==='focus'&&suppressFocusPreview)return;
  if(previewState?.pinned&&previewState.key!==item.key)return;
  showPreview(item,stride,anchor,false);
}
function showPreview(item,stride,anchor,pinned){
  cancelPreviewClose();
  if(previewState?.key===item.key){previewState.pinned ||= pinned;placePreview(anchor);return;}
  hidePreview();
  const title=item.marker?(item.marker.kind==='both'?'CV + VOC':item.marker.kind==='image'?'CV anomaly':'VOC anomaly'):`Image #${item.capture.number}`;
  const nearby=item.event?[item.event]:item.marker?events.filter(e=>{const c=cell(e.location);return c&&Math.floor(c.x/stride)===item.marker.x&&Math.floor(c.y/stride)===item.marker.y;}):[];
  const cv=nearby.find(e=>e.kind==='image'&&e.capture_id===item.capture_id);
  const voc=nearby.find(e=>e.kind==='voc');
  const capture=captures.find(c=>c.id===item.capture_id)||item.capture;
  const ratio=cv?.leaf_count?100*cv.infected_samples/cv.leaf_count:capture?.result?.leaf_count?100*capture.result.diseased_count/capture.result.leaf_count:null;
  const when=cv?.captured_at||voc?.measured_at||capture?.metadata?.captured_at||item.event?.created;
  const summary=[ratio!=null?`${percent(ratio)}% infected samples`:null,voc?`VOC +${num(voc.top_change)} ${esc(voc.units)}`:null].filter(Boolean);
  const note=$('marker-note');
  note.innerHTML=`<button id="close-marker" class="note-close" aria-label="Close anomaly preview">×</button><strong id="marker-preview-title">${esc(title)}</strong>${item.marker?.simulated||capture?.result?.simulated?'<span class="simulation-label">Includes simulation</span>':''}${item.capture_id?'<div class="preview-photo"><img id="marker-preview-photo" alt="Field photo for this anomaly" hidden><span id="preview-photo-status">Loading photo…</span></div>':''}${summary.length?`<p class="preview-summary">${summary.join('<br>')}</p>`:''}${when?`<time class="preview-time">${new Date(when).toLocaleTimeString([],{hour:'2-digit',minute:'2-digit'})}</time>`:''}${item.event&&!item.event.location?'<p class="preview-no-photo">Location unavailable</p>':''}${item.capture_id?'<button id="open-marker">Open image</button>':'<p class="preview-no-photo">No photo linked</p>'}`;
  previewState={key:item.key,anchor,pinned};note.hidden=false;
  note.setAttribute('role','region');note.setAttribute('aria-label','Anomaly preview');anchor.setAttribute('aria-describedby','marker-preview-title');
    $('close-marker').onclick=dismissPreview;
  if($('open-marker'))$('open-marker').onclick=()=>{lastMapPointKey=item.key;openCapture(item.capture_id);};
  placePreview(anchor);
  if(item.capture_id){
    const photo=$('marker-preview-photo'),status=$('preview-photo-status');
    photo.onload=()=>{photo.hidden=false;status.hidden=true;};
    photo.onerror=()=>{photo.hidden=true;status.hidden=false;status.textContent='Photo preview unavailable';};
    image(`/captures/${item.capture_id}/image`,item.capture_id,photo,{silent:true}).then(()=>{if(photo.isConnected&&!photo.getAttribute('src'))status.textContent='Photo preview unavailable';});
  }
}
function selectPoint(item,stride,anchor){
  lastMapPointKey=item.key;
  if(item.marker){filter={x:item.marker.x,y:item.marker.y,scale:stride};eventSignature='';renderEvents();}
  // A tap/click pins the same preview shown by hover or keyboard focus.
  showPreview(item,stride,anchor,true);
}
$('marker-note').addEventListener('pointerenter',cancelPreviewClose);
$('marker-note').addEventListener('pointerleave',schedulePreviewClose);
$('marker-note').addEventListener('focusin',cancelPreviewClose);
$('marker-note').addEventListener('focusout',schedulePreviewClose);
$('marker-note').addEventListener('click',e=>{if(previewState&&!e.target.closest('button'))previewState.pinned=true;});
  document.addEventListener('keydown',e=>{if(e.key==='Escape'&&!$('marker-note').hidden){e.preventDefault();dismissPreview();}});
function returnToMap(){
  const viewer=$('photo-viewer');if(viewer?.open)viewer.close();
  const saved=lastMapReturn;
  if(saved&&saved.base===base){filter=saved.filter;signal=saved.signal||'all';$('voc-panel').open=saved.vocOpen;$('cv-panel').open=saved.cvOpen;for(const input of document.querySelectorAll('input[name=signal]'))input.checked=input.value===signal;}
  tab('monitor');if(saved)map.restoreState(saved.map);renderGrid();eventSignature='';renderEvents();
  requestAnimationFrame(()=>{window.scrollTo({top:saved?.scroll||0,behavior:'instant'});const node=Array.from($('grid').querySelectorAll('[data-key]')).find(n=>n.dataset.key===saved?.key);node?.focus({preventScroll:true});});
}
async function openCapture(id){selected=id;detailSignature='';tab('capture');await renderDetail().catch(error=>notice(error.message,true));$('capture-detail').scrollIntoView({behavior:'instant',block:'start'});}
function renderEvents(){
  $('clear-filter').hidden=!filter;
  const inCell=e=>{if(!filter)return true;const c=cell(e.location);return c&&Math.floor(c.x/filter.scale)===filter.x&&Math.floor(c.y/filter.scale)===filter.y;};
  const shown=events.filter(inCell).filter(e=>signal==='all'||signal==='both'||e.kind===signal);
  const paired=e=>{if(signal!=='both')return true;const c=cell(e.location);const stride=map.getStride();return c&&groupMarkers(field?.cells||[],stride).some(p=>p.x===Math.floor(c.x/stride)&&p.y===Math.floor(c.y/stride)&&p.kind==='both');};
  const items=shown.filter(paired),voc=items.filter(e=>e.kind==='voc'),cv=items.filter(e=>e.kind==='image');
  $('event-summary').textContent=filter?'Selected area':`${field?.total_events||0} anomalies`;
  $('voc-count').textContent=voc.length;$('cv-count').textContent=cv.length;
  const signature=JSON.stringify([items,filter,signal]);if(signature===eventSignature)return;eventSignature=signature;
  for(const [kind,list] of [['voc',voc],['cv',cv]]){
    $(kind+'-events').innerHTML=list.length?list.map(e=>{
      const ratio=e.leaf_count?100*e.infected_samples/e.leaf_count:0;
      const title=e.kind==='image'?`${percent(ratio)}% infected`:`+${num(e.top_change)} ${esc(e.units)}`;
      return `<article class="anomaly-item">${e.simulated?'<span class="simulation-label">Simulated</span>':''}<div class="anomaly-row"><strong>${title}</strong><time datetime="${esc(e.created)}">${new Date(e.created).toLocaleTimeString([],{hour:'2-digit',minute:'2-digit'})}</time></div>${e.kind==='image'?`<button class="event-image-link" data-open-capture="${e.capture_id}">${e.infected_samples}/${e.leaf_count} leaf samples · View image</button>`:`<span class="anomaly-device">${esc(e.device)} · ${e.measurements} readings</span>`}<details class="event-facts"><summary>Details</summary><p>${e.location?`${e.location.lat.toFixed(5)}, ${e.location.lon.toFixed(5)}`:'No location'}</p><p>${e.kind==='image'?`Trigger: ${e.threshold}${e.image_rule==='percent'?'%':' samples'} · ${e.cache_expires?'photo cached':'cache expired'}`:`Differential rise ${num(e.difference_change)} ${esc(e.units)}`}</p></details></article>`;
    }).join(''):`<p class="empty">No ${kind==='cv'?'CV':'VOC'} anomalies${filter?' here':''}.</p>`;
    for(const node of $(kind+'-events').querySelectorAll('[data-open-capture]'))node.onclick=()=>openCapture(node.dataset.openCapture);
    for(const [index,node] of [...$(kind+'-events').querySelectorAll('.anomaly-item')].entries()){
      const event=list[index],item={key:`event-${event.id}`,event,marker:{kind:event.kind,simulated:event.simulated},capture_id:event.capture_id};
      node.tabIndex=0;
      node.setAttribute('aria-label',`${kind.toUpperCase()} anomaly. Preview details`);
      node.onpointerenter=e=>{if(e.pointerType!=='touch')showHoverPreview(item,map.getStride(),node,'hover');};
      node.onpointerleave=schedulePreviewClose;
      node.addEventListener('focusin',()=>showHoverPreview(item,map.getStride(),node,'focus'));
      node.addEventListener('focusout',schedulePreviewClose);
      node.onkeydown=e=>{if(e.target===node&&(e.key===' '||e.key==='Enter')){e.preventDefault();showPreview(item,map.getStride(),node,true);}};
    }
  }
}
function renderCaptures(){
  $('capture-count').textContent=`${captures.length} photos`;
  const signature=JSON.stringify(captures);if(signature===lastCaptures)return;lastCaptures=signature;
  $('captures').innerHTML=captures.length?captures.map(c=>`<tr><td><button data-capture="${c.id}">#${c.number}</button></td><td>${date(c.created)}</td><td>${esc(c.device)}</td><td>${esc(c.status.replaceAll('_',' '))}</td><td>${c.result?esc(candidateSummary(c.result)):'—'}${c.result?.simulated?' · SIMULATED':''}</td><td>${c.metadata?.location?`${c.metadata.location.lat.toFixed(5)}, ${c.metadata.location.lon.toFixed(5)}`:'Unmapped'}</td></tr>`).join(''):'<tr><td colspan="6">No images received. Open Image pipeline to send your first photo.</td></tr>';
  for(const button of $('captures').querySelectorAll('[data-capture]'))button.onclick=()=>openCapture(button.dataset.capture);
  renderGallery();
}
async function renderDetail(){
  const c=captures.find(c=>c.id===selected)||await(await request(`/captures/${selected}`)).json();
  const signature=JSON.stringify(c);if(signature===detailSignature)return;detailSignature=signature;
  const leafCount=c.leaves?.length||0,done=c.leaves?.filter(l=>l.result).length||0;
  $('capture-detail').innerHTML=`<div class="image-detail-heading"><h3>Image #${c.number} · ${esc(c.status.replaceAll('_',' '))}</h3><button id="back-to-map" class="image-close" aria-label="Return to previous map view" title="Return to map">×</button></div><p>${esc(c.progress?.message||'Waiting for processing')}</p>${pipelineBar(c,'detail-progress')}${leafCount?`<p class="muted">${done} of ${leafCount} leaf candidates analyzed</p>`:''}<p class="muted">Received ${date(c.created)}<br>Captured ${date(c.metadata?.captured_at)}<br>Threshold: ${c.metadata?.policy?.image_rule==='percent'?c.metadata.policy.infected_percent+'%':(c.metadata?.policy?.infected_samples??'legacy')+' samples'}${c.extraction?.truncated?' · candidate cap reached':''}</p>${c.error?`<p>${esc(c.error)}</p>`:''}<button id="enlarge-photo" class="text-button">Enlarge photo</button><img id="detail-photo" alt="Source field photo">${['failed','awaiting_model','awaiting_mask','no_candidates'].includes(c.status)?'<button id="retry">Retry processing</button>':''}<h3>${leafCount} leaf candidates${c.result?.simulated?' · SIMULATED':''}</h3><div class="leaves">${(c.leaves||[]).map(l=>`<div class="leaf"><img data-leaf="${l.id}" alt="Leaf candidate ${l.index}"><p>Candidate ${l.index} · ${esc(l.result?verdictLabel(l.result.verdict):l.status)}</p><p>${l.result?esc(scoreLabel(l.result)):''}</p></div>`).join('')}</div>`;
  applyBarWidths($('capture-detail'));
  image(`/captures/${c.id}/image`,c.id,$('detail-photo'));
  $('back-to-map').onclick=returnToMap;
  $('enlarge-photo').onclick=()=>showFullPhoto(c);
  $('detail-photo').onclick=()=>showFullPhoto(c);
  for(const img of $('capture-detail').querySelectorAll('[data-leaf]'))image(`/captures/${c.id}/leaves/${img.dataset.leaf}/image`,img.dataset.leaf,img);
  if($('retry'))$('retry').onclick=async()=>{try{await json(`/captures/${c.id}/retry`,{});notice('Image requeued.');detailSignature='';await refresh();}catch(error){notice(error.message,true);}};
}
function inputLocation(prefix){const a=$(`${prefix}-lat`).value,b=$(`${prefix}-lon`).value;if(!a&&!b)return null;if(!a||!b)throw new Error('Enter both latitude and longitude, or leave both blank');return{lat:Number(a),lon:Number(b)};}
$('photo').onchange=()=>{uploadKey=crypto.randomUUID();};
$('upload-form').onsubmit=async e=>{
  e.preventDefault();const photo=$('photo').files[0];if(!photo)return;
  if(photo.size>12*1024*1024)return notice('Choose a photo smaller than 12 MB.',true);
  const button=$('upload');button.disabled=true;button.textContent='Uploading…';
  try{const body=new FormData(),metadata={location:inputLocation('photo')};if($('captured-at').value)metadata.captured_at=new Date($('captured-at').value).toISOString();
    body.append('image',photo);body.append('device',$('photo-device').value);body.append('metadata',JSON.stringify(metadata));if($('mask').files[0])body.append('ground_mask',$('mask').files[0]);
    const c=await(await request('/captures',{method:'POST',body,headers:{'Idempotency-Key':uploadKey||=crypto.randomUUID()}})).json();selected=c.id;detailSignature='';notice(`Image #${c.number} accepted. Processing continues on the server.`);$('photo').value='';uploadKey=null;await refresh();
  }catch(error){notice(error.message,true);}finally{button.disabled=false;button.textContent='Upload and analyze';}
};
// A changed capture payload must receive a fresh retry key.
for(const id of ['mask','photo-device','captured-at','photo-lat','photo-lon'])$(id).onchange=()=>{uploadKey=crypto.randomUUID();};
$('rules-form').onsubmit=async e=>{e.preventDefault();try{const patch=Object.fromEntries(new FormData(e.target).entries());for(const key of Object.keys(patch))if(key!=='image_rule')patch[key]=Number(patch[key]);settings=await json('/settings',patch,'PUT');fillRules();notice('Event rules saved. New images use the updated threshold.');eventSignature='';await refresh();}catch(error){notice(error.message,true);}};
$('sensor-form').onsubmit=async e=>{e.preventDefault();try{await json('/telemetry/voc',{device:$('sensor-device').value,top:Number($('top').value),bottom:Number($('bottom').value),units:$('units').value,location:inputLocation('sensor'),sample_id:crypto.randomUUID(),measured_at:new Date().toISOString()});notice('Sensor measurement recorded.');await refresh();}catch(error){notice(error.message,true);}};
$('reset-baseline').onclick=async()=>{try{await json('/telemetry/reset',{device:$('sensor-device').value});notice('Baseline reset. The next reading becomes the reference.');}catch(error){notice(error.message,true);}};
function renderGas(){
  const g=(field?.gas||[])[0],card=$('gas-card');if(!card)return;
  if(!g){card.className='gas-card gas-idle';$('gas-status').textContent='No data';$('gas-reading').textContent='Waiting for hardware…';$('gas-fill').style.width='0%';$('gas-detail').textContent='Threshold set in Event rules.';return;}
  const stale=Date.now()-Date.parse(g.measured_at)>30000;
  card.className='gas-card gas-'+(stale?'idle':g.status);
  $('gas-status').textContent=stale?'Offline':g.status==='alert'?'GAS ALERT':g.status==='rising'?'Rising':'Normal';
  $('gas-reading').textContent=g.value!=null?`${num(g.value)} ${g.units}`:(g.alarm?'Digital alarm: gas detected':'Digital alarm: clear');
  const pct=g.value!=null&&g.threshold>0?Math.min(100,100*g.value/(g.threshold*1.5)):g.alarm?100:0;
  $('gas-fill').style.width=pct+'%';
  $('gas-detail').textContent=`${g.device} · threshold ${num(g.threshold)} · ${g.consecutive}/${g.hold} in a row · ${new Date(g.measured_at).toLocaleTimeString()}`;
}
function renderDrift(){
  const card=$('drift-card');if(!card)return;
  const all=field?.drift||[],d=all[0];
  const setBar=(name,v,th)=>{
    const fill=$(`drift-${name}-fill`),out=$(`drift-${name}`),full=(th.z_alarm||3)*1.5;
    for(const [cls,z] of [['drift-watch',th.z_watch||2],['drift-alarm',th.z_alarm||3]])fill.parentElement.querySelector('.'+cls).style.left=`${100*z/full}%`;
    if(!v||v.z_ewma==null){fill.style.width='0%';fill.className='';out.textContent='—';return;}
    fill.style.width=`${Math.max(0,Math.min(100,100*v.z_ewma/full))}%`;
    fill.className=v.z_ewma>=(th.z_alarm||3)?'z-alarm':v.z_ewma>=(th.z_watch||2)?'z-watch':'';
    const sl=v.slope_per_min!=null&&Math.abs(v.slope_per_min)>=.05?` ${v.slope_per_min>0?'↑':'↓'}`:'';
    out.textContent=`${v.z_ewma>=0?'+':''}${v.z_ewma.toFixed(1)}σ${sl} · S ${v.cusum.toFixed(1)}`;
  };
  if(!d){card.className='drift-card drift-idle';$('drift-status').textContent='No data';$('drift-verdict').textContent='Waiting for rover…';
    setBar('mq2',null,{});setBar('bme',null,{});$('drift-spark').innerHTML='';$('drift-detail').textContent='Calibrates on healthy air after warm-up.';return;}
  const stale=Date.now()-Date.parse(d.measured_at)>30000,th=d.thresholds||{};
  card.className='drift-card '+(stale?'drift-idle':d.status==='watch'?'drift-watch-state':'drift-'+d.status);
  $('drift-status').textContent=stale?'Offline':{waiting:'Warming up',calibrating:'Calibrating',healthy:'Healthy',watch:'Watch',diseased:'DISEASE?'}[d.status]||d.status;
  $('drift-verdict').textContent=d.status==='calibrating'?`Learning healthy air… ${Math.round(100*(d.calib_progress||0))}%`:
    d.status==='waiting'?'Sensors warming up':d.status==='diseased'?`Likely diseased · stress ${num(d.stress)}`:
    d.status==='watch'?`Drifting · stress ${num(d.stress)}`:`Plant air normal · stress ${num(d.stress??0)}`;
  setBar('mq2',d.sensors?.mq2,th);setBar('bme',d.sensors?.bme,th);
  // Sparkline: fused z over the latest snapshots from this device, oldest left.
  // Only the current detection run: stop at the latest (re)calibration.
  const pts=[];for(const x of all){if(x.device!==d.device)continue;if(!['healthy','watch','diseased'].includes(x.status))break;if(x.z!=null)pts.push(x);}
  pts.reverse();
  const top=Math.max((th.z_alarm||3)*1.5,...pts.map(p=>p.z)),bot=Math.min(-1,...pts.map(p=>p.z));
  const y=z=>(48-48*(z-bot)/(top-bot)).toFixed(1);
  const t0=pts.length?Date.parse(pts[0].measured_at):0,span=pts.length>1?Math.max(1,Date.parse(pts.at(-1).measured_at)-t0):1;
  const line=pts.map(p=>`${(200*(Date.parse(p.measured_at)-t0)/span).toFixed(1)},${y(p.z)}`).join(' ');
  $('drift-spark').innerHTML=`<line class="lim" x1="0" x2="200" y1="${y(th.z_watch||2)}" y2="${y(th.z_watch||2)}"/><line class="lim alarm" x1="0" x2="200" y1="${y(th.z_alarm||3)}" y2="${y(th.z_alarm||3)}"/>${pts.length>1?`<polyline points="${line}"/>`:''}`;
  $('drift-detail').textContent=`${d.device} · ${d.confidence==='low'?'one sensor only · ':''}CUSUM ${d.cusum!=null?d.cusum.toFixed(1):'—'}/${num(th.cusum_h??8)} · ${new Date(d.measured_at).toLocaleTimeString()}`;
}
function renderSensors(){
  const samples=field?.samples||[],latest=samples[0];$('sensor-summary').textContent=latest?`${latest.device} · top ${num(latest.top)}, bottom ${num(latest.bottom)} ${latest.units} · ${latest.consecutive}/${settings.voc_hold} hold`:'No measurements received.';
  $('samples').innerHTML=samples.length?samples.map(s=>`<tr><td>${date(s.measured_at)}<br>${esc(s.device)}</td><td>${num(s.top_change)}</td><td>${num(s.difference_change)}</td><td>${s.consecutive}/${settings.voc_hold}${s.event_id?' · event':''}</td></tr>`).join(''):'<tr><td colspan="4">Waiting for sensor data.</td></tr>';
  $('locations').innerHTML=(field?.locations||[]).map(l=>`<p>${esc(l.device)} · ${l.location.lat.toFixed(5)}, ${l.location.lon.toFixed(5)}<br>${date(l.measured_at)}</p>`).join('')||'No positions received.';
}
$('gps').onclick=()=>{
  if(gpsId!=null){stopGps();return;}if(!connected)return notice('Connect before starting GPS.',true);if(!navigator.geolocation)return notice('This browser does not support geolocation.',true);
  gpsDevice=$('sensor-device').value;
  gpsId=navigator.geolocation.watchPosition(async p=>{if(Date.now()-lastGps<1000)return;lastGps=Date.now();try{const loc={lat:p.coords.latitude,lon:p.coords.longitude};await json('/telemetry/location',{device:gpsDevice,location:loc,measured_at:new Date(p.timestamp).toISOString()});$('gps-status').textContent=`Streaming as ${gpsDevice} · accuracy ±${Math.round(p.coords.accuracy)} m`;}catch(error){notice(error.message,true);}},error=>{notice(`GPS: ${error.message}`,true);stopGps();},{enableHighAccuracy:true,maximumAge:0,timeout:20000});
  $('gps').textContent='Stop GPS stream';$('gps-status').textContent=`Waiting for GPS permission / fix · ${gpsDevice}`;
};
$('telemetry-file').onchange=async e=>{const file=e.target.files[0];if(!file)return;if(file.size>2e6)return notice('Keep telemetry imports below 2 MB.',true);let accepted=0;try{const text=await file.text();const data=text.trim().startsWith('[')?JSON.parse(text):text.split(/\r?\n/).filter(s=>s.trim()).map(s=>JSON.parse(s));if(!Array.isArray(data)||data.length>10000)throw new Error('Import an array or NDJSON with at most 10,000 rows');for(const row of data){if(!['voc','location'].includes(row.kind))throw new Error('Every row needs kind: voc or location');await json(`/telemetry/${row.kind}`,row);accepted++;}notice(`Imported ${accepted} telemetry rows.`);await refresh();}catch(error){notice(`${accepted} rows accepted. Import stopped: ${error.message}`,true);}finally{e.target.value='';}};
$('tools-toggle').onclick=()=>{const show=$('utility-menu').hidden;$('utility-menu').hidden=!show;$('tools-toggle').setAttribute('aria-expanded',String(show));};
for(const node of document.querySelectorAll('input[name="signal"]'))node.onchange=()=>{signal=node.value;eventSignature='';renderGrid();renderEvents();};
function syncDetection(){if(!settings)return;$('detection').disabled=!connected;const value=Math.min(80,Math.max(40,settings.infected_percent||40));const min=40,max=80;$('detection').min=min;$('detection').max=max;$('detection').value=value;$('detection-value').value=value+'%';$('range-min').textContent=min+'%';$('range-max').textContent=max+'%';$('detection-note').textContent=settings.image_rule==='count'?`Count rule: ${settings.infected_samples} samples · move slider to use %`:'Infected leaf samples · new photos';}
$('detection').oninput=()=>{$('detection-value').value=$('detection').value+'%';};
$('detection').onchange=async()=>{const slider=$('detection');slider.disabled=true;try{settings=await json('/settings',{image_rule:'percent',infected_percent:Number(slider.value)},'PUT');fillRules();notice(`Detection set to ${settings.infected_percent}% for new photos`);}catch(error){syncDetection();notice(error.message,true);}finally{slider.disabled=false;}};
function renderGallery(){
  const current=++galleryGeneration;
  $('image-gallery').innerHTML=captures.length?captures.map(c=>`<button class="image-tile" data-photo="${c.id}" aria-label="Open image ${c.number}"><img alt="Field image ${c.number}" loading="lazy"><span><strong>Image #${c.number}</strong><span>${date(c.created)}</span></span><span class="image-verdict">${c.result?.simulated?'Simulated · ':''}${c.result?esc(candidateSummary(c.result)):esc(c.status.replaceAll('_',' '))}</span>${pipelineBar(c,'tile-progress')}</button>`).join(''):'<p class="empty">Your field photos will appear here.</p>';
  applyBarWidths($('image-gallery'));
  // Thumbnails not fetched yet drive the loading bar; cached ones show at once.
  const nodes=[...$('image-gallery').querySelectorAll('[data-photo]')],total=nodes.filter(n=>!urls.has(n.dataset.photo)).length;
  let done=0;
  const label=()=>`Loading photos ${done}/${total}`;
  if(total)loading('photos',label(),0);else loading('photos',null,false);
  for(const node of nodes){
    node.onclick=()=>openCapture(node.dataset.photo);
    const fresh=!urls.has(node.dataset.photo);
    image(`/captures/${node.dataset.photo}/image`,node.dataset.photo,node.querySelector('img')).finally(()=>{
      if(!fresh||current!==galleryGeneration)return;
      done++;loading('photos',done<total?label():null,done<total?100*done/total:false);
    });
  }
}
function showFullPhoto(c){
  let dialog=$('photo-viewer');
  if(!dialog){dialog=document.createElement('dialog');dialog.id='photo-viewer';dialog.setAttribute('aria-label','Full field image');dialog.innerHTML='<button id="close-viewer" class="image-close" aria-label="Close image and return to map" title="Return to map">×</button><img id="full-photo" alt="Full field image">';document.body.append(dialog);$('close-viewer').onclick=returnToMap;dialog.addEventListener('cancel',e=>{e.preventDefault();returnToMap();});dialog.addEventListener('click',e=>{if(e.target===dialog)returnToMap();});}
  image(`/captures/${c.id}/image`,c.id,$('full-photo'));dialog.showModal();
}
function applyRoute(){const hash=location.hash.slice(1);if(hash.startsWith('images/')){const id=hash.slice(7);if(/^[a-z0-9-]+$/.test(id)&&connected&&selected!==id)openCapture(id);else tab('capture');}else if(hash==='images')tab('capture');else if(['sensors','rules','rover'].includes(hash))tab(hash);else tab('monitor');}
window.addEventListener('hashchange',applyRoute);
renderGrid();
if(token)connect().catch(error=>notice(error.message,true));
setInterval(()=>refresh(),3000);
if('serviceWorker'in navigator)navigator.serviceWorker.register('/sw.js').catch(()=>{});
