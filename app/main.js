import './style.css';
const $ = id => document.getElementById(id);
let base = localStorage.getItem('leaflink-url') || '';
let token = sessionStorage.getItem('leaflink-token') || localStorage.getItem('leaflink-token') || '';
let connected = false, selectedFile, uploadKey, selectedId, captures = [], previewUrl, previewId, refreshing = false;
let selectedMask, leafSignature = '', cropUrls = new Map();
let lastContact=0,pollFailed=false,uploading=false,uploadStarted=0,uploadPercent=null;
let uploadError='';
const labels = { queued:'Queued', uploading:'Saving to storage', splitting:'Separating leaf candidates', processing:'Analyzing leaf candidates', awaiting_model:'Waiting for model', awaiting_mask:'Waiting for ground mask', no_candidates:'No leaf candidates found', failed:'Needs attention', complete:'Complete' };
$('server-url').value = base;
$('api-token').value = token;
$('remember').checked = Boolean(localStorage.getItem('leaflink-token'));
function notice(message, error = false) { $('notice').textContent = message; $('notice').classList.toggle('error', error); }
function showSettings(show) { $('settings').hidden = !show; $('settings-toggle').setAttribute('aria-expanded', String(show)); }
$('settings-toggle').onclick = () => showSettings($('settings').hidden);
async function api(route, options = {}) {
  const response = await fetch(`${base}/api${route}`, { ...options, headers: { Authorization: `Bearer ${token}`, ...options.headers }, signal: AbortSignal.timeout(30000) });
  if (!response.ok) { const body = await response.json().catch(() => ({})); throw new Error(body.error || `Server returned ${response.status}`); }
  return response;
}
async function connect() {
  const settings = await (await api('/config')).json();
  lastContact=Date.now();pollFailed=false;
  connected = true;
  $('connection-dot').classList.add('online');
  $('pipeline-mode').textContent = `${settings.storage === 'b2' ? 'B2' : 'LOCAL TEST'} / ${settings.model === 'http' ? 'MODEL' : settings.model.toUpperCase()}`;
  $('mask-field').hidden = settings.leaf_extraction !== 'heuristic';
  $('storage-description').textContent = settings.storage === 'b2' ? 'Your image passes through private B2 storage.' : 'Local test storage replaces B2 in this environment.';
  notice(`${settings.storage === 'b2' ? 'Backblaze B2 configured.' : 'Local test storage active; Backblaze B2 is not connected.'} ${settings.leaf_extraction === 'heuristic' ? settings.ground_mask === 'provided' ? 'Upload a SegFormer-B2 ground mask to separate leaves.' : 'SegFormer-B2 and the leaf heuristic prepare individual crops.' : 'Leaf extraction disabled.'} ${settings.model === 'disabled' ? 'Crops will wait for your disease model.' : settings.model === 'demo' ? 'Demo mode: every verdict is simulated, without a disease model.' : 'Disease model service configured.'}`);
  await refresh();
}
$('connection-form').onsubmit = async event => {
  event.preventDefault();
  let nextBase = $('server-url').value.trim().replace(/\/+$/, '');
  if (nextBase) {
    try { const url = new URL(nextBase); if (!['http:','https:'].includes(url.protocol) || url.username || url.password || url.search || url.hash || url.pathname !== '/') throw new Error(); }
    catch { return notice('Enter a server origin such as https://leaf.example.com, without a path.', true); }
  }
  base = nextBase; token = $('api-token').value.trim();
  localStorage.setItem('leaflink-url', base);
  sessionStorage.setItem('leaflink-token', token);
  if ($('remember').checked) localStorage.setItem('leaflink-token', token); else localStorage.removeItem('leaflink-token');
  selectedId = null; captures = []; previewId = null;
  try { await connect(); showSettings(false); }
  catch(error) { connectionError(error); }
};
function connectionError(error) {
  connected = false; $('connection-dot').classList.remove('online');
  notice(`${error.message}. Check the URL, token, network and server CORS origins.`, true);
  pollFailed=true;renderActivity();
}
$('disconnect').onclick = () => {
  token = ''; sessionStorage.removeItem('leaflink-token'); localStorage.removeItem('leaflink-token');
  $('api-token').value = ''; connected = false; captures = []; selectedId = null;
  selectedFile = null; uploadKey = null; previewId = null; $('send-photo').hidden = true;
  selectedMask = null; $('ground-mask').value = '';
  lastContact=0;pollFailed=false;renderActivity();
  $('connection-dot').classList.remove('online'); $('pipeline-mode').textContent = 'NOT CONNECTED';
  renderList(); renderJourney(); clearPreview(); notice('Disconnected. Connect your server to start capturing.');
};
function clearPreview() {
  if (previewUrl) URL.revokeObjectURL(previewUrl);
  previewUrl = null; $('preview').removeAttribute('src'); $('preview').hidden = true; $('capture-hint').hidden = false;
}
function setPreview(blob) { clearPreview(); previewUrl = URL.createObjectURL(blob); $('preview').src = previewUrl; $('preview').hidden = false; $('capture-hint').hidden = true; }
$('take-photo').onclick = () => $('camera').click();
$('choose-photo').onclick = () => $('library').click();
for (const id of ['camera','library']) $(id).onchange = event => {
  const file = event.target.files[0]; if (!file) return;
  if (file.size > 12 * 1024 * 1024) return notice('Choose an image smaller than 12 MB.', true);
  if (!['image/jpeg','image/png','image/webp'].includes(file.type)) return notice('Choose JPEG, PNG or WebP. Export HEIC photos as JPEG first.', true);
  selectedFile = file; uploadKey = typeof crypto.randomUUID === 'function' ? crypto.randomUUID() : Array.from(crypto.getRandomValues(new Uint8Array(16)), byte => byte.toString(16).padStart(2,'0')).join(''); selectedId = null; previewId = null;
  uploadError='';
  selectedMask = null; $('ground-mask').value = '';
  setPreview(file); $('send-photo').hidden = false; renderJourney();
};
$('ground-mask').onchange = event => {
  const file = event.target.files[0];
  if (file && (file.type !== 'image/png' || file.size > 12*1024*1024)) { event.target.value = ''; selectedMask = null; return notice('Choose a binary PNG ground mask smaller than 12 MB.',true); }
  selectedMask = file;
  uploadKey = Array.from(crypto.getRandomValues(new Uint8Array(16)),byte=>byte.toString(16).padStart(2,'0')).join('');
};
$('send-photo').onclick = async () => {
  if (!connected) { showSettings(true); return notice('Connect your server before uploading.', true); }
  if (!selectedFile) return;
  const button = $('send-photo'); button.disabled = true; button.textContent = 'Uploading…';
  uploading=true;uploadError='';uploadStarted=Date.now();uploadPercent=null;renderActivity();
  try {
    const body = new FormData(); body.append('image', selectedFile); body.append('device', 'phone');
    if (selectedMask) body.append('ground_mask',selectedMask);
    const capture = await uploadCapture(body,uploadKey);
    selectedId = capture.id; selectedFile = null; button.hidden = true;
    $('camera').value = ''; $('library').value = '';
    selectedMask = null; $('ground-mask').value = '';
    await refresh();
  } catch(error) { uploadError=error.message;notice(error.message, true); }
  finally { uploading=false;button.disabled = false; button.textContent = 'Send for analysis ↗';renderActivity(); }
};
function uploadCapture(body,key) {
  return new Promise((resolve,reject)=>{
    const xhr=new XMLHttpRequest();xhr.open('POST',`${base}/api/captures`);xhr.timeout=180000;
    xhr.setRequestHeader('Authorization',`Bearer ${token}`);xhr.setRequestHeader('Idempotency-Key',key);
    xhr.upload.onprogress=event=>{uploadPercent=event.lengthComputable?Math.round(event.loaded/event.total*100):null;renderActivity();};
    xhr.onerror=()=>reject(new Error('Upload connection lost. Keep this photo selected and retry; duplicate protection will reuse the capture.'));
    xhr.ontimeout=()=>reject(new Error('Upload timed out. Retry the same photo when the connection improves.'));
    xhr.onload=()=>{try{const body=JSON.parse(xhr.responseText);if(xhr.status<200||xhr.status>=300)return reject(new Error(body.error||`Upload failed (${xhr.status})`));lastContact=Date.now();pollFailed=false;resolve(body);}catch{reject(new Error('Invalid upload response'));}};
    xhr.send(body);
  });
}
async function refresh() {
  if (!connected || refreshing) return;
  refreshing = true;
  try {
    captures = await (await api('/captures')).json();
    lastContact=Date.now();pollFailed=false;
    if (!selectedId && !selectedFile && captures.length) selectedId = captures[0].id;
    renderList(); renderJourney();
    if (selectedId && !selectedFile && previewId !== selectedId) {
      const id = selectedId;
      const blob = await (await api(`/captures/${encodeURIComponent(id)}/image`)).blob();
      if (id === selectedId && !selectedFile) { setPreview(blob); previewId = id; }
    }
  } catch(error) {pollFailed=true;throw error;} finally { refreshing = false;renderActivity(); }
}
function renderList() {
  $('capture-list').replaceChildren();
  if (!captures.length) { const p = document.createElement('p'); p.className = 'empty'; p.textContent = 'Your next observation starts here. Phone and Raspberry Pi captures appear together.'; $('capture-list').append(p); return; }
  for (const row of captures) {
    const button = document.createElement('button'); button.className = `capture-row${row.id === selectedId ? ' selected' : ''}`;
    const icon = document.createElement('span'); icon.className = 'row-icon'; icon.textContent = row.device === 'phone' ? '↗' : '▦';
    const text = document.createElement('span'); text.className = 'row-text';
    const strong = document.createElement('strong'); strong.textContent = row.device;
    const small = document.createElement('small'); small.textContent = `${new Date(row.created).toLocaleString()} · ${row.id.slice(0,8)}`;
    const status = document.createElement('span'); status.className = 'row-status'; status.textContent = row.result ? `${row.result.simulated ? 'DEMO · ' : ''}${row.result.scope === 'leaf_candidates' ? `${row.result.diseased_count}/${row.result.leaf_count} flagged` : row.result.verdict === 'healthy' ? 'Healthy' : 'Diseased'}` : labels[row.status] || row.status;
    text.append(strong,small); button.append(icon,text,status);
    button.onclick = () => { selectedId = row.id; selectedFile = null; $('send-photo').hidden = true; refresh().catch(error => notice(error.message, true)); };
    $('capture-list').append(button);
  }
}
function renderJourney() {
  const row = captures.find(r => r.id === selectedId);
  const stages = ['capture','storage','leaves','model','verdict'];
  let progress = row ? (row.status === 'complete' ? 5 : row.extraction ? 3 : row.stored ? 2 : 1) : selectedFile ? 1 : 0;
  for (let i=0; i<stages.length; i++) {
    const node = $(`step-${stages[i]}`); node.classList.toggle('done', i < progress); node.classList.toggle('active', Boolean(row) && !['failed','no_candidates','complete'].includes(row.status) && i === progress);
    node.querySelector('.step-mark').textContent = i < progress ? '✓' : '';
  }
  const result = $('result'); result.className = 'result'; result.replaceChildren();
  const symbol = document.createElement('span'); symbol.className = 'result-symbol'; symbol.textContent = '↗';
  const content = document.createElement('div'); const title = document.createElement('h3'); const detail = document.createElement('p');
  title.textContent = row ? labels[row.status] || row.status : 'Ready when you are';
  detail.textContent = row ? `Capture ${row.id.slice(0,8)} · ${row.storage === 'b2' ? 'Backblaze B2' : 'local test storage'}` : 'Your capture’s progress and result will appear here.';
  if (row?.result) {
    const r = row.result; result.classList.add(r.simulated ? 'simulated' : r.verdict);
    title.textContent = `${r.simulated ? 'DEMO · ' : ''}${r.verdict === 'healthy' ? 'Healthy' : 'Diseased'}`;
    detail.textContent = r.simulated ? 'Simulated result for pipeline testing. No ML model analyzed this image.' : `Model score: ${(r.confidence*100).toFixed(1)}% · ${r.model_version}. Scores may be uncalibrated.`;
    if (r.scope === 'leaf_candidates') {
      title.textContent = `${r.simulated ? 'DEMO · ' : ''}${r.diseased_count} of ${r.leaf_count} candidates flagged`;
      detail.textContent = r.simulated ? 'Simulated disease verdicts. Leaf crops are real; no disease classifier ran.' : 'Results describe only the detected candidates. Missed leaves and the whole plant have not been assessed.';
    }
  }
  if (row?.status === 'awaiting_model') detail.textContent = row.extraction ? `${row.leaves.length} leaf candidates saved. Connect your disease model, then retry this capture.` : 'Image saved. Connect your ML service on the server, then retry this capture.';
  if (row?.status === 'awaiting_mask') detail.textContent = 'A binary ground-ignore mask is needed. Upload one below or configure local SegFormer-B2, then retry.';
  if (row?.status === 'no_candidates') detail.textContent = 'The heuristic found no usable leaf candidates. No disease verdict was assigned. Try a closer, sharper image; brown leaves may be missed.';
  if (row?.status === 'failed' || row?.status === 'queued' && row.error) detail.textContent = row.error;
  content.append(title,detail);
  if (row && ['failed','awaiting_model','awaiting_mask','no_candidates'].includes(row.status)) {
    const retry = document.createElement('button'); retry.className = 'secondary'; retry.textContent = 'Retry capture';
    retry.onclick = async () => { retry.disabled = true; try { await api(`/captures/${row.id}/retry`, { method:'POST' }); await refresh(); } catch(error) { notice(error.message,true); retry.disabled = false; } };
    content.append(retry);
    const maskLabel = document.createElement('label'); maskLabel.className = 'mask-field'; maskLabel.textContent = 'Supply / replace binary ground mask';
    const maskInput = document.createElement('input'); maskInput.type = 'file'; maskInput.accept = 'image/png';
    maskInput.onchange = async () => {
      if (!maskInput.files[0]) return;
      maskInput.disabled = true;
      try { const body = new FormData(); body.append('ground_mask',maskInput.files[0]); await api(`/captures/${row.id}/ground-mask`,{method:'POST',body}); await refresh(); }
      catch(error) {notice(error.message,true);maskInput.disabled = false;}
    };
    maskLabel.append(maskInput); content.append(maskLabel);
  }
  result.append(symbol,content);
  renderLeaves(row);
  renderActivity();
}
function renderLeaves(row) {
  const signature = JSON.stringify([row?.id,row?.status,row?.extraction,row?.leaves]);
  if (signature === leafSignature) return;
  leafSignature = signature;
  const id = row?.id;
  for (const [key,url] of cropUrls) if (!key.startsWith(`${id}/`)) {if (url) URL.revokeObjectURL(url);cropUrls.delete(key);}
  $('leaf-grid').replaceChildren();
  $('leaf-section').hidden = !row?.extraction;
  if (!row?.extraction) return;
  $('leaf-heading').textContent = `${row.leaves.length} leaf candidate${row.leaves.length === 1 ? '' : 's'}`;
  $('leaf-note').textContent = `Heuristic proposals can merge or fragment foliage and miss brown leaves. Ground overlap is not detection confidence.${row.extraction.truncated ? ` Showing the first ${row.leaves.length} of ${row.extraction.total_candidates} proposals due to the configured limit.` : ''}`;
  for (const leaf of row.leaves) {
    const card = document.createElement('article'); card.className = 'leaf-card';
    const img = document.createElement('img'); img.alt = `Leaf candidate ${leaf.index}`; img.dataset.leafid = leaf.id;
    const title = document.createElement('h3'); title.textContent = `Candidate ${leaf.index}`;
    const meta = document.createElement('p'); meta.textContent = `Source bounds: ${leaf.bounds.join(', ')} · Ground overlap ${(leaf.ground_overlap*100).toFixed(1)}%`;
    const verdict = document.createElement('p'); verdict.className = 'leaf-verdict';
    verdict.textContent = leaf.result ? `${leaf.result.simulated ? 'DEMO · ' : ''}${leaf.result.verdict === 'healthy' ? 'Healthy' : 'Diseased'} · Model score ${(leaf.result.confidence*100).toFixed(1)}%${leaf.result.simulated ? ' (simulated)' : ''}` : leaf.status==='processing' ? 'Analyzing this leaf…' : row.status==='awaiting_model' ? 'Waiting for disease model' : 'Queued for analysis';
    if (leaf.result?.class_name) {const label=document.createElement('p');label.className='leaf-class';label.textContent=leaf.result.class_name.replaceAll('_',' ');card.append(label);}
    card.append(img,title,meta,verdict); $('leaf-grid').append(card);
    const key = `${id}/${leaf.id}`;
    if (cropUrls.get(key)) img.src = cropUrls.get(key);
    else if (!cropUrls.has(key)) {
      cropUrls.set(key,null);
      api(`/captures/${id}/leaves/${leaf.id}/image`).then(response=>response.blob()).then(blob=>{
        if (selectedId !== id) {cropUrls.delete(key);return;}
        const url = URL.createObjectURL(blob);cropUrls.set(key,url);
        for (const current of $('leaf-grid').querySelectorAll('img')) if (current.dataset.leafid === leaf.id) current.src = url;
      }).catch(()=>{cropUrls.delete(key);img.alt = `Candidate ${leaf.index} preview unavailable`;});
    }
  }
}
function renderActivity() {
  const row=captures.find(row=>row.id===selectedId);
  const stale=lastContact && Date.now()-lastContact>12000;
  const offline=navigator.onLine===false;
  const p=row?.progress;
  const stages={queued:1,storage:1,ground_mask:2,leaf_separation:3,saving_crops:4,classifying:4,complete:6,awaiting_mask:2,awaiting_model:4,no_candidates:4};
  const active=uploading || Boolean(row && ['queued','uploading','splitting','processing'].includes(row.status));
  const terminal=row && ['complete','failed','no_candidates'].includes(row.status);
  let title='Ready for a field scan',message='Take a photo or choose an image to start.',step=0;
  if(!connected){title='Connect your server';message='Open Connection and enter the server URL and token.';}
  if(row){step=stages[p?.stage]??(row.stored?2:1);title=labels[row.status]||row.status;message=p?.message||'Waiting for the next processing step.';}
  if(p?.stage==='ground_mask'){title='Identifying ground';message=p.message;}
  if(p?.stage==='leaf_separation'){title='Separating leaf candidates';message=p.message;}
  if(p?.stage==='classifying'&&p.total_leaves){title=`Analyzing leaf ${p.current_leaf} of ${p.total_leaves}`;message=`${p.completed_leaves} completed · Disease model running on the server.`;}
  if(row?.status==='queued'&&row.error){title=`Retrying · attempt ${Math.min(row.attempts+1,3)} of 3`;message='A processing step failed. The saved capture is waiting for automatic retry.';}
  if(row?.status==='failed'){title='Scan needs attention';message=row.error||'Select Retry capture to try again.';}
  if(uploading){title=uploadPercent===100?'Upload sent · waiting for server':'Uploading photo';message=uploadPercent===null?'Keep this page open while the photo is sent.':`${uploadPercent}% transferred · Keep this page open.`;step=0;}
  if(uploadError&&!uploading){title='Upload interrupted';message=uploadError;}
  if(offline){title='Offline · connection lost';message='No live updates. Keep the page open; it will reconnect. A capture already accepted by the server continues there.';}
  else if(connected&&(pollFailed||stale)){title='Reconnecting to server';message='Showing the last known step. A capture already accepted may still be running on the server.';}
  const panel=$('activity');panel.classList.toggle('is-running',active&&!offline&&!pollFailed&&!stale);panel.classList.toggle('is-offline',Boolean(offline||pollFailed||stale||uploadError));panel.classList.toggle('is-complete',row?.status==='complete'&&!uploading&&!offline&&!pollFailed&&!stale);
  if($('activity-title').textContent!==title)$('activity-title').textContent=title;
  if($('activity-message').textContent!==message)$('activity-message').textContent=message;
  $('activity-progress').value=step;
  const started=uploading?uploadStarted:row?Date.parse(row.started||row.created):0;
  const end=terminal?Date.parse(p?.updated_at||row.created):Date.now();
  const seconds=started?Math.max(0,Math.floor((end-started)/1000)):0;
  $('activity-time').textContent=started?`${Math.floor(seconds/60)}:${String(seconds%60).padStart(2,'0')} elapsed`:'Ready';
  $('activity-step').textContent=uploading?'Step 1 of 6 · Upload':row?`Step ${Math.min(step+1,6)} of 6${p?.total_leaves?` · ${p.completed_leaves||0}/${p.total_leaves} leaves complete`:''}`:'Upload → store → mask → separate → classify → results';
  $('activity-contact').textContent=lastContact?`Server seen ${Math.floor((Date.now()-lastContact)/1000)}s ago`:'No server contact yet';
}
$('refresh').onclick = () => refresh().catch(error => notice(error.message,true));
setInterval(renderActivity,1000);
setInterval(() => { if (connected && !document.hidden) refresh().catch(error => notice(`Refresh failed: ${error.message}`,true)); }, 2000);
window.addEventListener('offline',renderActivity);
window.addEventListener('online',()=>{renderActivity();if(connected)refresh().catch(()=>{});});
document.addEventListener('visibilitychange',()=>{if(!document.hidden&&connected)refresh().catch(()=>{});});
if (token) connect().catch(connectionError); else showSettings(true);
if ('serviceWorker' in navigator) navigator.serviceWorker.register('/sw.js').catch(() => {});
