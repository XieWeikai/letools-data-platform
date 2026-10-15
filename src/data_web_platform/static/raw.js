/* Raw MCAP viewer: one authoritative clock, one decoded quad, four canvases. */
(() => {
  "use strict";
  const $ = id => document.getElementById(id), T = window.RawTimeline;
  const colors = ["#2358a8", "#b03a48", "#16806e", "#8955b3", "#a66a12", "#147b9d", "#6b7325", "#b44d8b", "#526b8b", "#754c27", "#28702d", "#8b4658", "#3b51a2", "#17696d", "#77537b", "#645d28"];
  const video = $("raw-video");
  const state = {records:[], manifest:null, id:null, selected:new Map(), charts:new Map(), clock:"record",
    time:0, playing:false, duration:0, range:[0,1], generation:0, plotGeneration:0, sampleGeneration:0,
    listingGeneration:0, poll:null, raf:null, frameRequest:null, wall:0, sampleAt:0, sampleBusy:false,
    canvases:[], active:false, videoError:null, presentedTime:null, pendingSeek:null};
  const element = (tag, cls, text) => { const el = document.createElement(tag); if(cls) el.className=cls; if(text !== undefined) el.textContent=text; return el; };
  const svg = (tag, attrs) => { const el=document.createElementNS("http://www.w3.org/2000/svg",tag); for(const [key,value] of Object.entries(attrs)) el.setAttribute(key,String(value)); return el; };
  const put = (id,text) => { $(id).textContent=text; };
  const showError = error => { $("raw-error").hidden=!error; put("raw-error", error?.message || error || ""); };
  const fmt = value => Number.isFinite(value) ? value.toLocaleString("en-US",{maximumFractionDigits:4}) : "—";
  const videoInfo = () => state.manifest?.video[state.clock];
  const clamp = time => Math.max(0,Math.min(state.duration,time));
  async function api(path, body) {
    const response=await fetch(path,body ? {method:"POST",headers:{"Content-Type":"application/json"},body:JSON.stringify(body)} : {});
    const result=await response.json();
    if(!response.ok) throw new Error(typeof result.detail === "string" ? result.detail : `请求失败 (${response.status})`);
    return result;
  }
  function dispose() {
    pause(); clearTimeout(state.poll); state.poll=null;
    if(state.frameRequest !== null && video.cancelVideoFrameCallback) video.cancelVideoFrameCallback(state.frameRequest);
    state.frameRequest=null; state.plotGeneration++; state.sampleGeneration++;
    state.videoError=null;state.presentedTime=null;state.pendingSeek=null; video.removeAttribute("src"); video.load();
  }
  function filterRecordings() {
    const query=$("raw-filter").value.trim().toLowerCase(), selected=$("raw-recording").value;
    const records=state.records.filter(item=>item.name.toLowerCase().includes(query));
    $("raw-recording").replaceChildren(...records.map(item=>{const option=element("option","",item.name+(item.incomplete?" · 元数据不完整":"")); option.value=item.path; return option;}));
    if(records.some(r=>r.path===selected)) $("raw-recording").value=selected;
    $("raw-recording").disabled=!records.length; $("raw-open").disabled=!records.length;
    if(!records.length) $("raw-recording").append(element("option","","没有匹配的录制"));
  }
  async function loadRecordings() {
    const generation=++state.listingGeneration, path=$("raw-root").value.trim();
    showError(null); $("raw-load").disabled=true; $("raw-open").disabled=true;
    try {
      const result=await api(`/api/recordings?${new URLSearchParams({path,provider:"tianji"})}`);
      if(generation!==state.listingGeneration) return;
      state.records=result.recordings; $("raw-root").value=result.root;
      try {localStorage.setItem("dwp-raw-root",result.root);} catch {}
      filterRecordings(); put("raw-status",`找到 ${state.records.length} 条录制；选择一条并打开。`);
    } catch(error) {if(generation===state.listingGeneration) {state.records=[];filterRecordings();showError(error);}}
    finally {if(generation===state.listingGeneration) $("raw-load").disabled=false;}
  }
  async function openRecording() {
    const path=$("raw-recording").value;
    if(!path || !state.records.some(r=>r.path===path)) return;
    const generation=++state.generation; dispose(); showError(null); state.manifest=null; state.id=null;
    $("raw-open").disabled=true; $("raw-content").hidden=true; $("raw-empty").hidden=true;
    put("raw-status","正在打开录制。首次读取会建立 topic 索引并封装原始视频，原文件保持不变…");
    async function consume(result) {
      if(generation!==state.generation) return;
      state.id=result.id;
      if(result.state==="ready") {ready(result.manifest); $("raw-open").disabled=false;return;}
      if(["failed","interrupted"].includes(result.state)) throw new Error(result.message);
      put("raw-status",`${result.message || "建立预览索引"}${result.messages ? ` · 已读取 ${result.messages.toLocaleString()} 条消息` : ""}`);
      state.poll=setTimeout(async()=>{try {await consume(await api(`/api/raw/${state.id}`));} catch(error) {failed(error);}},700);
    }
    function failed(error) {if(generation===state.generation) {showError(error);put("raw-status","未能打开录制，可重试或选择其他轨迹。");$("raw-open").disabled=false;$("raw-empty").hidden=false;}}
    try {await consume(await api("/api/raw/open",{path}));} catch(error) {failed(error);}
  }
  function defaultFields(topic) {
    let fields=topic.fields.filter(f=>f.startsWith("position[") || f.startsWith("positions[") || f.startsWith("wrench.") || f.startsWith("pose.position."));
    if(!fields.length) fields=topic.fields.filter(f=>!f.startsWith("layout."));
    return fields.slice().sort((a,b)=>a.localeCompare(b,undefined,{numeric:true})).slice(0,14);
  }
  function commonTopics() {
    if(!state.manifest) return;
    state.selected.clear();
    const names=["/tj/joint_states","/tj/info/wrench_left","/tj/info/wrench_right"];
    for(const topic of state.manifest.topics) if(names.includes(topic.name)) state.selected.set(topic.id,defaultFields(topic));
    if(!state.selected.size) for(const topic of state.manifest.topics.filter(t=>t.fields.length).slice(0,3)) state.selected.set(topic.id,defaultFields(topic));
    renderTopics(); renderCharts();
  }
  function ready(manifest) {
    state.manifest=manifest; state.clock=$("raw-clock").value; state.time=0;
    $("raw-content").hidden=false; $("raw-empty").hidden=true;
    $("raw-source-options").open=false;
    put("raw-name",manifest.name); put("raw-counts",`${manifest.topics.length} 个 topic · ${manifest.messages.toLocaleString()} 条原始消息`);
    put("raw-status","原始预览已就绪；播放、拖动时间条或点击曲线，同步查看四路画面和原始消息。");
    $("raw-notes").replaceChildren();
    for(const topic of manifest.topics) {
      const lines=[...topic.errors];
      if(!topic.count) lines.push("没有可读取消息（空 topic 保留显示，不填 state）");
      if(topic.invalid_values) lines.push(`${topic.invalid_values} 个缺失或非有限数值，图表断开显示`);
      if(topic.clocks.header.reversals || topic.clocks.record.reversals) lines.push("存在时间戳回退；浏览曲线按所选时间排序，原始消息未修正");
      if(topic.header_offset_ms && Math.max(Math.abs(topic.header_offset_ms.min),Math.abs(topic.header_offset_ms.max))>1000) lines.push("Header 与记录时钟偏差超过 1 秒，请谨慎比较；未自动校正");
      if(lines.length) $("raw-notes").append(element("p","raw-notice",`${topic.name}：${lines.join("；")}`));
    }
    state.canvases=[]; $("raw-camera-grid").replaceChildren();
    for(const camera of manifest.cameras) {
      const card=element("figure","raw-camera"), caption=element("figcaption",""), label=element("span","",camera.label);
      const expand=element("button","","放大"); expand.type="button";expand.setAttribute("aria-label",`放大${camera.label}画面`);
      expand.addEventListener("click",()=>{if(document.fullscreenElement) document.exitFullscreen();else card.requestFullscreen?.().catch(showError);});
      caption.append(label,expand);
      const box=element("div","raw-camera-image"), canvas=element("canvas",""), status=element("div","raw-camera-status","等待视频");
      canvas.setAttribute("aria-label",`${camera.label}原始画面`);canvas.width=960;canvas.height=540;
      box.append(canvas,status);card.append(caption,box);$("raw-camera-grid").append(card);
      state.canvases.push({camera,canvas,status,ctx:canvas.getContext("2d",{alpha:false})});
    }
    setupClock(); commonTopics();
  }
  function setupClock() {
    pause(); state.time=0; state.duration=state.manifest.clocks[state.clock].duration; state.range=[0,Math.max(.001,state.duration)];
    state.videoError=null;state.presentedTime=null; $("raw-seek").max=state.duration; $("raw-at").max=state.duration;
    const origin=state.manifest.clocks[state.clock].origin_ns;
    put("raw-origin",`${new Date(Number(BigInt(origin)/1000000n)).toLocaleString("zh-CN",{hour12:false})} · ${origin} ns`);
    put("raw-clock-note",state.clock==="record" ? "所有 topic 使用 MCAP 记录时间；横轴为距整条录制起点的秒数，保留各路开始/结束差异。" : "优先使用消息 Header；无有效 Header 时使用记录时间。未估算时钟偏移或传输延迟，请结合 topic 偏差说明查看。");
    const info=videoInfo();
    if(info) {state.pendingSeek=0;video.src=`/api/raw/${state.id}/video/${state.clock}`;video.load();}
    else {state.pendingSeek=null;video.removeAttribute("src");video.load();}
    $("raw-prev").disabled=$("raw-next").disabled=!info;
    const errors=state.manifest.video.errors;
    $("raw-video-error").hidden=Boolean(info)&&!errors.length;
    put("raw-video-error",errors.join("；") || "未找到可播放的四宫格视频，数值与文本 topic 仍可独立浏览。");
    updateTime(0); renderTopics(); renderCharts(); drawCameras();
  }
  function renderTopics() {
    if(!state.manifest) return;
    const query=$("raw-topic-filter").value.toLowerCase();
    put("raw-topic-count",`${state.selected.size} / ${state.manifest.topics.length}`);
    $("raw-topics").replaceChildren();
    for(const topic of state.manifest.topics) {
      if(!`${topic.name} ${topic.type}`.toLowerCase().includes(query)) continue;
      const box=element("div","raw-topic"), label=element("label","raw-topic-label"), check=element("input","");
      check.type="checkbox";check.checked=state.selected.has(topic.id);check.setAttribute("aria-label",`显示 ${topic.name}`);
      const info=element("span","raw-topic-name",topic.name), timing=topic.clocks[state.clock];
      info.append(element("small","",topic.type),element("small","",`${topic.count.toLocaleString()} 条 · ${timing.hz?`${fmt(timing.hz)} Hz` : "无频率"}${!topic.count?" · 空 topic":""}`));
      label.append(check,info);box.append(label);
      check.addEventListener("change",()=>{
        if(check.checked && state.selected.size>=12) {check.checked=false;showError("最多同时显示 12 个 topic，请先取消其他信号。");return;}
        if(check.checked) state.selected.set(topic.id,defaultFields(topic));else state.selected.delete(topic.id);
        showError(null);renderTopics();renderCharts();
      });
      if(check.checked && topic.fields.length) {
        const details=element("details","raw-fields"), summary=element("summary","",`选择字段 (${state.selected.get(topic.id).length}/${topic.fields.length})`), list=element("div","raw-field-list");
        for(const field of topic.fields.slice().sort((a,b)=>a.localeCompare(b,undefined,{numeric:true}))) {
          const row=element("label",""), input=element("input","");input.type="checkbox";input.checked=state.selected.get(topic.id).includes(field);
          input.addEventListener("change",()=>{
            let fields=state.selected.get(topic.id);
            if(input.checked && fields.length>=16) {input.checked=false;showError("每张图最多显示 16 个字段。");return;}
            fields=input.checked?[...fields,field]:fields.filter(f=>f!==field);state.selected.set(topic.id,fields);
            summary.textContent=`选择字段 (${fields.length}/${topic.fields.length})`;renderCharts();
          });row.append(input,element("span","",field));list.append(row);
        }
        details.append(summary,list);box.append(details);
      }
      $("raw-topics").append(box);
    }
  }
  async function renderCharts() {
    const generation=++state.plotGeneration;state.sampleGeneration++;state.charts.clear();$("raw-charts").replaceChildren();
    if(!state.manifest) return;
    put("raw-range",`显示 ${state.range[0].toFixed(3)} – ${state.range[1].toFixed(3)} s；所有图使用同一横轴。`);
    if(!state.selected.size) {$("raw-charts").append(element("p","empty-state","在左侧选择要查看的 topic。"));return;}
    for(const [token,fields] of state.selected) {
      const topic=state.manifest.topics.find(t=>t.id===token), box=element("section","raw-chart"), title=element("h3","",topic.name);
      const timing=topic.clocks[state.clock], meta=element("div","raw-chart-meta");
      meta.append(element("span","",`${topic.count.toLocaleString()} 条 · 最大间隔 ${fmt(timing.max_gap_ms)} ms`),element("span","",topic.header_count?`Header ${topic.header_count}/${topic.count}`:"使用记录时间"));
      const current=element("div","raw-current","读取当前时刻的原始消息…"), message=element("details","raw-message"), pre=element("pre","",""), summary=element("summary","","查看当前时刻之前的最近原始消息");
      message.append(summary,pre);box.append(title,meta);
      const chart={topic,fields,box,current,pre,summary,plot:null,cursor:null,data:null};state.charts.set(token,chart);
      if(fields.length && topic.count) {
        const plot=svg("svg",{class:"raw-plot",role:"img",tabindex:"0","aria-label":`${topic.name} 时间曲线；点击定位，方向键微调`});
        chart.plot=plot;box.append(plot);
        plot.addEventListener("click",event=>{const rect=plot.getBoundingClientRect(), width=rect.width; seek(state.range[0]+Math.max(0,Math.min(1,(event.clientX-rect.left-56)/(width-72)))*(state.range[1]-state.range[0]));});
        plot.addEventListener("keydown",event=>{if(["ArrowLeft","ArrowRight"].includes(event.key)){event.preventDefault();seek(state.time+(event.key==="ArrowLeft"?-1:1)*.02);}});
        const legend=element("div","raw-legend");fields.forEach((field,i)=>{const item=element("span",""), swatch=element("i","raw-swatch");swatch.style.background=colors[i%colors.length];item.append(swatch,document.createTextNode(field));legend.append(item);});box.append(legend);
        const params=new URLSearchParams({topic:token,clock:state.clock,start:state.range[0],end:state.range[1],limit:1800});fields.forEach(f=>params.append("fields",f));
        api(`/api/raw/${state.id}/series?${params}`).then(data=>{
          if(generation!==state.plotGeneration) return;chart.data=data;drawPlot(chart);
          meta.append(element("span","",data.reduced?"概览：原始极值选点，放大查看细节":"当前区间全部原始点"));
        }).catch(error=>{if(generation===state.plotGeneration) box.append(element("p","raw-notice",error.message));});
      } else box.append(element("p","field-help",!topic.count?"此 topic 没有消息。未补值或填入 state。":"此 topic 以原始消息显示；可在左侧选择可绘图字段。"));
      if(topic.header_offset_ms) box.append(element("p","field-help",`Header − 记录时间：${fmt(topic.header_offset_ms.min)} 至 ${fmt(topic.header_offset_ms.max)} ms`));
      box.append(current,message);$("raw-charts").append(box);
    }
    await updateSamples(true);
  }
  function drawPlot(chart) {
    if(!chart.plot||!chart.data) return;
    const width=Math.max(240,chart.plot.getBoundingClientRect().width), height=180, left=56,right=width-16,top=12,bottom=150;
    chart.width=width;chart.plot.setAttribute("viewBox",`0 0 ${width} ${height}`);chart.plot.replaceChildren();
    const data=chart.data, finite=data.values.flat().filter(v=>v!==null&&Number.isFinite(v));
    let low=finite.length?finite.reduce((a,b)=>Math.min(a,b),Infinity):0,high=finite.length?finite.reduce((a,b)=>Math.max(a,b),-Infinity):1;
    const pad=high===low?Math.max(1,Math.abs(high)*.1):(high-low)*.08;low-=pad;high+=pad;
    const x=time=>left+(time-state.range[0])/(state.range[1]-state.range[0])*(right-left), y=value=>bottom-(value-low)/(high-low)*(bottom-top);
    const defs=svg("defs",{}),clip=svg("clipPath",{id:`raw-clip-${chart.topic.id}`});clip.append(svg("rect",{x:left,y:top,width:right-left,height:bottom-top}));defs.append(clip);chart.plot.append(defs);
    for(let i=0;i<=4;i++) {
      const yy=top+(bottom-top)*i/4,xx=left+(right-left)*i/4;
      chart.plot.append(svg("line",{x1:left,x2:right,y1:yy,y2:yy,class:"raw-gridline"}));
      const value=svg("text",{x:left-7,y:yy+3,"text-anchor":"end"});value.textContent=fmt(high-(high-low)*i/4);chart.plot.append(value);
      const time=svg("text",{x:xx,y:172,"text-anchor":"middle"});time.textContent=(state.range[0]+(state.range[1]-state.range[0])*i/4).toFixed(2);chart.plot.append(time);
    }
    const group=svg("g",{"clip-path":`url(#raw-clip-${chart.topic.id})`});
    chart.fields.forEach((_,i)=>{group.append(svg("path",{d:T.paths(data.times,data.values,i,data.gap_threshold,x,y,data.breaks),fill:"none",stroke:colors[i%colors.length],"stroke-width":1.3,"vector-effect":"non-scaling-stroke"}));});
    chart.cursor=svg("line",{x1:x(state.time),x2:x(state.time),y1:top,y2:bottom,class:"raw-playhead"});group.append(chart.cursor);chart.plot.append(group);
  }
  function valueAt(sample,path) {return path.replace(/\[(\d+)\]/g,".$1").split(".").reduce((v,key)=>v?.[key],sample);}
  async function updateSamples(force=false) {
    if(!state.id||!state.selected.size||state.sampleBusy&&!force) return;
    const generation=++state.sampleGeneration,at=state.time,params=new URLSearchParams({clock:state.clock,at});
    if(force) for(const chart of state.charts.values()) chart.current.textContent=`读取 ${at.toFixed(3)} s 对应的原始消息…`;
    for(const token of state.selected.keys()) params.append("topics",token);
    state.sampleBusy=true;
    try {
      const samples=await api(`/api/raw/${state.id}/samples?${params}`);
      if(generation!==state.sampleGeneration) return;
      for(const item of samples) {
        const chart=state.charts.get(item.id);if(!chart) continue;
        if(item.sample===null){chart.current.textContent="此时刻之前没有消息";chart.pre.textContent="无消息";continue;}
        const stale=item.age_ms>chart.topic.clocks[state.clock].gap_threshold*1000;
        chart.current.textContent=`${item.outside?"已超过本 topic 的结束时间 · ":stale?"暂无新样本 · ":""}最近真实消息 ${item.time.toFixed(6)} s · 距游标 ${item.age_ms.toFixed(3)} ms\n`+
          chart.fields.map(field=>`${field}: ${fmt(valueAt(item.sample,field))}`).join("   ");
        chart.pre.textContent=JSON.stringify(item.sample,null,2);
        chart.summary.textContent=`原始消息 · record ${item.record_ns} ns`;
      }
    } catch(error) {if(generation===state.sampleGeneration) showError(error);}
    finally {if(generation===state.sampleGeneration) state.sampleBusy=false;}
  }
  function updateTime(time) {
    state.time=clamp(time);$("raw-seek").value=state.time;
    put("raw-time",`${state.time.toFixed(3)} / ${state.duration.toFixed(3)} s`);
    if(document.activeElement!==$("raw-at")) $("raw-at").value=state.time.toFixed(3);
    for(const chart of state.charts.values()) if(chart.cursor) {
      const x=56+(state.time-state.range[0])/(state.range[1]-state.range[0])*(chart.width-72);
      chart.cursor.setAttribute("x1",x);chart.cursor.setAttribute("x2",x);
    }
    const info=videoInfo(), frame=T.frameAt(info,state.time);
    const displayed=info&&state.presentedTime!==null?T.previous(info.times,state.presentedTime+1e-6):frame.index;
    put("raw-video-time",frame.outside?"当前时刻不在视频覆盖内":`视频帧 ${displayed+1}/${info.frames} · 帧时间 ${(info.times[displayed]??0).toFixed(6)} s · 游标 ${state.time.toFixed(3)} s`);
    for(const camera of state.canvases) {
      const reason=state.videoError || (!info?"本录制无可用视频":frame.outside?"当前时刻没有视频":video.seeking?"正在定位画面…":video.readyState<2?"视频正在加载…":frame.age>.1?`暂无新视频帧 · 已间隔 ${(frame.age*1000).toFixed(0)} ms`:"");
      camera.status.hidden=!reason;camera.status.textContent=reason;
    }
  }
  function drawCameras() {
    if(video.readyState<2||video.seeking||!video.videoWidth) return;
    const w=video.videoWidth/2,h=video.videoHeight/2;
    for(const {camera,canvas,ctx} of state.canvases) {
      if(canvas.width!==w||canvas.height!==h){canvas.width=w;canvas.height=h;}
      ctx.drawImage(video,camera.column*w,camera.row*h,w,h,0,0,w,h);
    }
  }
  function scheduleFrame() {
    if(!video.requestVideoFrameCallback || state.frameRequest!==null) return;
    state.frameRequest=video.requestVideoFrameCallback((_,metadata)=>{state.frameRequest=null;const info=videoInfo();state.presentedTime=info?info.offset+metadata.mediaTime:null;drawCameras();updateTime(state.time);if(state.playing) scheduleFrame();});
  }
  function seek(time) {
    if(!state.manifest || !Number.isFinite(time)) return;
    const target=clamp(time),info=videoInfo();state.wall=performance.now();
    state.pendingSeek=info?target:null;
    if(info && video.readyState>=1) {
      const media=Math.max(0,Math.min(info.end-info.offset,target-info.offset));
      // Browser media positions are commonly quantized to microseconds. Bias
      // by 2 us so a request at an exact nanosecond frame boundary selects
      // that frame, not the preceding one. The logical cursor is unchanged.
      scheduleFrame();
      if(Math.abs(video.currentTime-media)>.000001 || video.seeking) video.currentTime=media+.000002;
      else state.pendingSeek=null;
    }
    updateTime(target);drawCameras();updateSamples(true);if(state.playing) syncPlayback();
  }
  function syncPlayback() {
    const info=videoInfo(), inside=info&&state.time>=info.offset&&state.time<info.end;
    if(state.playing&&inside&&!state.videoError&&state.pendingSeek===null&&video.readyState>=2) {
      video.playbackRate=Number($("raw-speed").value);
      if(video.paused) video.play().then(scheduleFrame).catch(error=>{state.videoError=`视频无法播放：${error.message}`;pause();updateTime(state.time);});
    } else video.pause();
  }
  function tick(now) {
    if(!state.playing) return;
    const dt=Math.min(.2,(now-state.wall)/1000)*Number($("raw-speed").value);state.wall=now;
    const info=videoInfo(),inside=info&&state.time>=info.offset&&state.time<info.end&&!state.videoError;
    let next=inside?(state.pendingSeek!==null||video.seeking||video.readyState<2?state.time:Math.min(info.end,video.currentTime+info.offset)):state.time+dt;
    if(info&&!inside&&state.time<info.offset&&next>=info.offset) {next=info.offset;video.currentTime=0;}
    updateTime(next);syncPlayback();if(!video.requestVideoFrameCallback) drawCameras();
    if(now-state.sampleAt>250){state.sampleAt=now;updateSamples();}
    if(state.time>=state.duration){pause();updateSamples(true);return;}
    state.raf=requestAnimationFrame(tick);
  }
  function play() {
    if(!state.manifest) return;
    if(state.time>=state.duration) seek(0);
    state.playing=true;put("raw-play","暂停");$("raw-play").setAttribute("aria-pressed","true");state.wall=performance.now();syncPlayback();scheduleFrame();state.raf=requestAnimationFrame(tick);
  }
  function pause() {
    state.playing=false;video.pause();cancelAnimationFrame(state.raf);state.raf=null;put("raw-play","播放");$("raw-play").setAttribute("aria-pressed","false");
  }
  function zoom(factor) {
    if(!state.manifest) return;
    const width=Math.min(state.duration,Math.max(.02,(state.range[1]-state.range[0])*factor));
    const left=Math.max(0,Math.min(state.duration-width,state.time-width/2));state.range=[left,left+width];renderCharts();
  }
  $("raw-load").addEventListener("click",loadRecordings);$("raw-open").addEventListener("click",openRecording);
  $("raw-filter").addEventListener("input",filterRecordings);$("raw-topic-filter").addEventListener("input",renderTopics);
  $("raw-root").addEventListener("input",()=>{state.listingGeneration++;state.records=[];filterRecordings();$("raw-load").disabled=false;});
  $("raw-root").addEventListener("keydown",event=>{if(event.key==="Enter"){event.preventDefault();loadRecordings();}});
  document.addEventListener("raw-directory-selected",loadRecordings);
  $("raw-topics-common").addEventListener("click",commonTopics);
  $("raw-topics-clear").addEventListener("click",()=>{state.selected.clear();renderTopics();renderCharts();});
  $("raw-play").addEventListener("click",()=>state.playing?pause():play());
  $("raw-seek").addEventListener("input",()=>seek(Number($("raw-seek").value)));
  $("raw-go").addEventListener("click",()=>seek(Number($("raw-at").value)));
  $("raw-at").addEventListener("keydown",event=>{if(event.key==="Enter") seek(Number($("raw-at").value));});
  $("raw-prev").addEventListener("click",()=>{pause();seek(T.step(videoInfo(),state.time,-1,state.duration));});
  $("raw-next").addEventListener("click",()=>{pause();seek(T.step(videoInfo(),state.time,1,state.duration));});
  $("raw-clock").addEventListener("change",()=>{state.clock=$("raw-clock").value;if(state.manifest) setupClock();});
  $("raw-speed").addEventListener("change",syncPlayback);
  $("raw-zoom-in").addEventListener("click",()=>zoom(.5));$("raw-zoom-out").addEventListener("click",()=>zoom(2));
  $("raw-zoom-reset").addEventListener("click",()=>{if(state.manifest){state.range=[0,Math.max(.001,state.duration)];renderCharts();}});
  video.addEventListener("loadeddata",()=>{drawCameras();updateTime(state.time);});
  video.addEventListener("loadedmetadata",()=>{const info=videoInfo();if(info){scheduleFrame();video.currentTime=Math.max(0,Math.min(info.end-info.offset,state.time-info.offset))+.000002;}});
  video.addEventListener("seeked",()=>{state.pendingSeek=null;drawCameras();updateTime(state.time);scheduleFrame();syncPlayback();});
  video.addEventListener("error",()=>{if(video.getAttribute("src")){state.videoError="视频加载或解码失败，请重新打开录制。数值仍可浏览。";updateTime(state.time);}});
  document.addEventListener("visibilitychange",()=>{if(document.hidden) pause();});
  document.addEventListener("platform-view-change",event=>{
    state.active=event.detail==="raw";if(!state.active) pause();else requestAnimationFrame(()=>{for(const chart of state.charts.values()) drawPlot(chart);});
  });
  let resizeTimer;new ResizeObserver(()=>{clearTimeout(resizeTimer);resizeTimer=setTimeout(()=>{for(const chart of state.charts.values()) drawPlot(chart);},120);}).observe($("raw-charts"));
  window.addEventListener("beforeunload",dispose);
  try {$("raw-root").value=localStorage.getItem("dwp-raw-root")||"";}catch {}
  api("/api/environment").then(env=>{if(!$("raw-root").value) $("raw-root").value=env.default_source||"";}).catch(()=>{});
})();
