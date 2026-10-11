/* Local dataset editor. User text is rendered with textContent, never HTML. */
(() => {
  'use strict';
  const $ = id => document.getElementById(id);
  const el = (tag, text, cls) => { const n = document.createElement(tag); if (text != null) n.textContent = text; if (cls) n.className = cls; return n; };
  const state = {source:null, selected:new Set(), tasks:{}, deleted:new Set(), offset:0, rows:[], generation:0, revision:0, plan:null, active:null, busy:false, available:false, timer:null, browserSequence:0};
  const system = new Set(['timestamp','frame_index','episode_index','index','task_index']);
  const active = new Set(['queued','running']);
  const stage = {queued:'等待执行',preflight:'检查输入',editor_plan:'计算编辑计划',editor_apply:'编辑表格、视频与统计',validate:'验证输出',publish:'发布结果'};
  function error(value) { $('editor-error').hidden = !value; $('editor-error').textContent = value || ''; if(value) $('editor-error').focus(); }
  async function api(path, body) {
    const response = await fetch(path, {cache:'no-store', ...(body ? {method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(body)} : {})});
    const data = await response.json();
    if(!response.ok) throw Error(Array.isArray(data.detail) ? data.detail.map(i=>`${i.loc?.slice(1).join('.')}: ${i.msg}`).join('；') : data.detail || `HTTP ${response.status}`);
    return data;
  }
  function changed() { state.revision++; state.plan=null; $('apply-edit').disabled=true; summary(); }
  function summary() {
    $('selection-count').textContent=`${state.selected.size} 已选择`;
    const features=document.querySelectorAll('#feature-options input:checked').length;
    $('edit-summary').textContent=`修改 ${Object.keys(state.tasks).length} 条 task · 移除 ${state.deleted.size} 条轨迹 · 移除 ${features} 个字段${$('video-enabled').checked?' · 处理视频':''}`;
    $('preview-edit').disabled=!state.source || !state.available || state.busy;
    $('apply-edit').disabled=!state.plan || state.busy;
  }
  function renderRows() {
    const body=$('episode-rows'); body.replaceChildren();
    for(const row of state.rows){
      const tr=el('tr'); const check=el('input'); check.type='checkbox'; check.checked=state.selected.has(row.index); check.setAttribute('aria-label',`选择 episode ${row.index}`);
      check.onchange=()=>{if(check.checked)state.selected.add(row.index);else state.selected.delete(row.index);summary();};
      const c1=el('td');c1.append(check); const c2=el('td',`#${row.index}`);c2.append(el('small',`${row.length} 帧 · ${row.seconds.toFixed(2)} s`));
      const c3=el('td');c3.append(el('small',`原任务：${row.tasks.join(' / ')}`));
      if(state.deleted.has(row.index)){tr.className='removed';c3.append(el('strong','将从新输出移除'));}
      else {const input=el('input');input.type='text';input.maxLength=4096;input.value=state.tasks[row.index]??'';input.placeholder='留空保留原任务';input.setAttribute('aria-label',`episode ${row.index} 的新任务`);input.oninput=()=>{if(input.value.trim())state.tasks[row.index]=input.value;else delete state.tasks[row.index];changed();};c3.append(input);}
      if(state.deleted.has(row.index)||Object.hasOwn(state.tasks,row.index)){const undo=el('button','撤销','text-button');undo.type='button';undo.onclick=()=>{state.deleted.delete(row.index);delete state.tasks[row.index];changed();renderRows();};c3.append(undo);}
      tr.append(c1,c2,c3);body.append(tr);
    }
    $('page-label').textContent=`${state.offset+1}–${Math.min(state.offset+state.rows.length,state.source.episodes)} / ${state.source.episodes} 条`;
    $('episode-prev').disabled=state.offset===0;$('episode-next').disabled=state.offset+state.rows.length>=state.source.episodes;summary();
  }
  async function loadSource(offset=0, reset=false) {
    const id=$('editor-dataset').value;if(!id)return;
    if(state.busy) throw Error('请等待当前任务结束后切换数据集');
    const generation=++state.generation;error('');
    if(reset){state.source=null;state.plan=null;state.tasks={};state.deleted.clear();state.selected.clear();summary();}
    const data=await api(`/api/editor/inspect?${new URLSearchParams({dataset:id,offset,limit:50})}`);
    if(generation!==state.generation)return;
    if(!reset&&state.source&&data.source_fingerprint!==state.source.source_fingerprint){state.plan=null;summary();throw Error('源数据已变化，请重新加载后编辑');}
    state.source=data;state.rows=data.episode_preview;state.offset=offset;
    $('source-summary').textContent=`${data.path} · ${data.version} · ${data.episodes} 条 · ${data.frames} 帧 · ${data.fps} FPS`;
    if(reset){
      $('editor-output').value=`${data.path}_edited_${new Date().toISOString().replace(/[-:T.]/g,'').slice(0,14)}`;
      $('feature-options').replaceChildren();$('camera-options').replaceChildren();
      for(const [key,feature] of Object.entries(data.features)){
        const label=el('label');const input=el('input');input.type='checkbox';input.value=key;input.disabled=system.has(key);
        label.append(input,el('span',`${key} · ${feature.dtype}${input.disabled?'（系统字段）':''}`));$('feature-options').append(label);
        input.onchange=()=>{const camera=[...document.querySelectorAll('#camera-options input')].find(n=>n.value===key);if(camera){camera.disabled=input.checked;if(input.checked)camera.checked=false;}changed();};
        if(feature.dtype==='video'){const c=el('input');c.type='checkbox';c.value=key;c.onchange=changed;const l=el('label');l.append(c,el('span',key));$('camera-options').append(l);}
      }
    }
    renderRows();
  }
  const checked = id => [...document.querySelectorAll(`#${id} input:checked`)].map(n=>n.value);
  const integer = (id, optional=false) => {const text=$(id).value;if(optional&&text==='')return null;const v=Number(text);if(!Number.isInteger(v))throw Error('请输入整数');return v;};
  function request(operation='plan'){
    if(!state.source)throw Error('请先选择数据集');
    if(!$('editor-output').value.startsWith('/'))throw Error('请填写新数据集的绝对路径');
    let video=null;
    if($('video-enabled').checked){
      const width=integer('video-width',true),height=integer('video-height',true);
      if((width===null)!==(height===null))throw Error('宽度和高度必须同时填写或同时留空');
      video={codec:$('video-codec').value,size:width===null?null:[width,height],pixel_format:$('pixel-format').value||null,video_keys:checked('camera-options')};
      if(video.codec==='libx264'){video.crf=integer('video-crf');video.preset=$('video-preset').value;}else video.quality=integer('video-quality');
    }
    return {dataset:state.source.id,output:$('editor-output').value.trim(),operation,source_fingerprint:state.source.source_fingerprint,
      task_by_episode:{...state.tasks},delete_episodes:[...state.deleted].sort((a,b)=>a-b),remove_features:checked('feature-options'),video,
      workers:integer('editor-workers',true),codec_threads:integer('codec-threads',true),batch_rows:integer('batch-rows'),
      ...(operation==='apply'?{plan_job_id:state.plan.id}:{})};
  }
  function renderPlan(plan){
    const box=$('plan-summary');box.replaceChildren();box.hidden=!plan;if(!plan)return;
    for(const [key,label] of Object.entries({input_episodes:'原轨迹',episodes:'输出轨迹',frames:'输出帧',data_files_rewrite:'重写表格文件',files_reuse:'复用文件',videos_remux:'重封装视频',videos_transcode:'转码视频',workers:'实际 workers',codec_threads:'编码器线程'})){
      const cell=el('div');cell.append(el('strong',String(plan[key]??'—')),el('span',label));box.append(cell);
    }
    $('plan-warnings').replaceChildren(...(plan.warnings||[]).map(w=>el('li',w)));
  }
  async function poll(){
    const id=state.active;if(!id)return;
    try{
      const job=await api(`/api/jobs/${id}`);if(state.active!==id)return;
      $('job-status').textContent=`${({queued:'排队中',running:'运行中',succeeded:'已完成',failed:'失败',cancelled:'已取消',interrupted:'已中断'})[job.status]||job.status} · ${stage[job.stage]||job.stage} · ${Number(job.elapsed_seconds||0).toFixed(1)} 秒${job.stage==='editor_apply'&&active.has(job.status)?'（视频与统计按文件处理，当前无精确百分比）':''}`;
      $('editor-logs').textContent=(job.logs||[]).join('\n');$('cancel-edit').hidden=!active.has(job.status);
      if(active.has(job.status)){state.timer=setTimeout(poll,1500);return;}
      state.busy=false;$('editor-dataset').disabled=false;$('reload-source').disabled=false;
      $('editor-report').textContent=JSON.stringify(job.result||job,null,2);
      const links=$('editor-result-links');links.replaceChildren();const report=el('a','下载 JSON 报告','button secondary');report.href=`/api/jobs/${id}/report`;links.append(report);
      if(job.status==='succeeded'){
        renderPlan(job.result.plan);
        if(job.request.operation==='plan'&&state.pendingRevision===state.revision)state.plan={id};
        else if(job.request.operation==='apply'){
          state.plan=null;const dataset=job.result.dataset;
          if(dataset){const open=el('a','查看编辑后的数据集','button primary');open.href=`/viewer/local/${encodeURIComponent(dataset.id)}/0`;links.append(open);const edit=el('a','继续编辑新数据集','button secondary');edit.href=`/editor?dataset=${encodeURIComponent(dataset.id)}`;links.append(edit);}
          else{const path=el('p',`输出已生成：${job.result.destination}；可返回工作台登记。`);links.append(path);}
        }
      }else{state.plan=null;error(job.error||'任务未完成，请查看日志');}
      summary();
    }catch(e){error(e.message);state.timer=setTimeout(poll,4000);}
  }
  async function submit(operation){
    if(state.busy)return;error('');
    try{
      const body=request(operation);
      state.pendingRevision=state.revision;
      state.busy=true;summary();$('editor-dataset').disabled=true;$('reload-source').disabled=true;
      const job=await api('/api/editor/jobs',body);state.active=job.id;state.plan=null;
      $('editor-report').textContent='';$('editor-result-links').replaceChildren();$('editor-logs').textContent='';renderPlan(null);
      $('job-status').textContent='任务已提交';$('editor-job').scrollIntoView({block:'start'});poll();
    }catch(e){state.busy=false;$('editor-dataset').disabled=false;$('reload-source').disabled=false;error(e.message);summary();}
  }
  function withError(fn){return async(...args)=>{try{await fn(...args);}catch(e){error(e.message);}};}
  $('editor-form').onsubmit=e=>{e.preventDefault();submit('plan');};$('apply-edit').onclick=()=>submit('apply');
  $('editor-dataset').onchange=withError(()=>loadSource(0,true));$('reload-source').onclick=withError(()=>loadSource(0,true));
  $('episode-prev').onclick=withError(()=>loadSource(Math.max(0,state.offset-50)));$('episode-next').onclick=withError(()=>loadSource(state.offset+50));
  $('select-page').onclick=()=>{state.rows.forEach(r=>state.selected.add(r.index));renderRows();};
  $('select-all-episodes').onclick=withError(()=>{if(!state.source)return;if(state.source.episodes>100000)throw Error('请分批选择，最多 100000 条');for(let i=0;i<state.source.episodes;i++)state.selected.add(i);renderRows();});
  $('clear-episodes').onclick=()=>{state.selected.clear();renderRows();};
  $('set-tasks').onclick=withError(()=>{if(!state.selected.size)throw Error('请先选择轨迹');const value=$('bulk-task').value;if(!value.trim())throw Error('任务文本不能为空');if([...state.selected].some(id=>state.deleted.has(id)))throw Error('选择中含待删除轨迹，请先撤销删除');for(const id of state.selected)state.tasks[id]=value;changed();renderRows();});
  $('delete-episodes').onclick=withError(()=>{if(!state.selected.size)throw Error('请先选择轨迹');if(new Set([...state.deleted,...state.selected]).size>=state.source.episodes)throw Error('至少保留一条轨迹');for(const id of state.selected){state.deleted.add(id);delete state.tasks[id];}changed();renderRows();});
  $('reset-episodes').onclick=()=>{for(const id of state.selected){state.deleted.delete(id);delete state.tasks[id];}changed();renderRows();};
  $('import-tasks').onclick=withError(()=>{if(!state.source)throw Error('请先选择数据集');const value=JSON.parse($('task-json').value);if(!value||Array.isArray(value)||typeof value!=='object')throw Error('请输入 JSON 对象');for(const [key,text] of Object.entries(value)){if(!/^(0|[1-9][0-9]*)$/.test(key)||Number(key)>=state.source.episodes||typeof text!=='string'||!text.trim()||text.length>4096||state.deleted.has(Number(key)))throw Error(`无效或待删除的 episode/task：${key}`);}Object.assign(state.tasks,value);changed();renderRows();});
  $('video-enabled').onchange=()=>{$('video-controls').hidden=!$('video-enabled').checked;changed();};
  $('video-codec').onchange=()=>{$('h264-options').hidden=$('video-codec').value!=='libx264';$('mjpeg-options').hidden=$('video-codec').value!=='mjpeg';changed();};
  for(const id of ['editor-output','video-width','video-height','video-crf','video-preset','video-quality','pixel-format','editor-workers','codec-threads','batch-rows'])$(id).addEventListener('input',changed);
  $('cancel-edit').onclick=withError(async()=>{if(!state.active)return;await api(`/api/jobs/${state.active}/cancel`,{});poll();});
  let browseState=null;
  async function browse(path){const seq=++state.browserSequence;$('browse-error').textContent='';$('use-output').disabled=true;try{const data=await api(`/api/browse?${new URLSearchParams({path})}`);if(seq!==state.browserSequence)return;browseState=data;$('browse-path').value=data.path;$('browse-parent').disabled=!data.parent;$('browse-directories').replaceChildren(...data.directories.map(d=>{const b=el('button',d.name,'folder-entry');b.type='button';b.onclick=()=>browse(d.path);return b;}));$('use-output').disabled=false;}catch(e){$('browse-error').textContent=e.message;}}
  $('browse-output').onclick=()=>{$('output-dialog').showModal();browse(state.source?.path.split('/').slice(0,-1).join('/')||'/home/user');};
  $('close-output').onclick=()=>{$('output-dialog').close();++state.browserSequence;};
  $('browse-path-form').onsubmit=e=>{e.preventDefault();browse($('browse-path').value);};$('browse-parent').onclick=()=>browse(browseState.parent);
  $('use-output').onclick=()=>{const name=$('output-name').value.trim();if(!name||name==='.'||name==='..'||/[\/\\\x00-\x1f]/.test(name)){$('browse-error').textContent='请输入单个新目录名称';return;}$('editor-output').value=browseState.path.replace(/\/$/,'')+'/'+name;$('output-dialog').close();changed();};
  withError(async()=>{const [options,datasets]=await Promise.all([api('/api/editor/options'),api('/api/datasets')]);state.available=options.available;$('editor-status').textContent=options.available?`Editor ${options.version}`:'Editor 不可用';if(!options.available)error(options.error);$('editor-dataset').replaceChildren(...datasets.map(d=>{const o=el('option',`${d.name} · ${d.info.codebase_version}`);o.value=d.id;return o;}));if(!datasets.length){$('source-summary').textContent='请先在工作台登记一个 LeRobot 数据集';return;}const requested=new URLSearchParams(location.search).get('dataset');if(requested&&datasets.some(d=>d.id===requested||d.repo_id===requested))$('editor-dataset').value=requested.replace(/^local\//,'');await loadSource(0,true);})();
})();
