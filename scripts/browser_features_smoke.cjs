/* Additional read-only viewer coverage and one anomaly-check job; no conversion. */
const {execFileSync} = require('node:child_process');
const fs = require('node:fs');
const path = require('node:path');
const assert = require('node:assert/strict');
const {chromium} = require(process.env.PLAYWRIGHT_MODULE || 'playwright');

async function verifyTab(page,tab,dataset,entry,out) {
  if(tab==='Statistics') {
    const labels=[['ROBOT TYPE',dataset.info.robot_type],['DATASET VERSION',dataset.info.codebase_version],
      ['TOTAL FRAMES',dataset.info.total_frames],['TOTAL EPISODES',dataset.info.total_episodes],['FPS',dataset.info.fps]];
    for(const [label,value] of labels) {
      assert(value!==undefined,`fixture metadata missing ${label}`);
      const lines=entry.text.split('\n').map(line=>line.trim()).filter(Boolean);
      const at=lines.indexOf(label);
      assert(at>=0,`statistics label ${label} missing`);
      assert.equal(lines[at+1].replaceAll(',',''),String(value),`statistics ${label} must match dataset metadata`);
    }
    const cameras=Object.entries(dataset.info.features).filter(([,feature])=>feature.dtype==='video');
    assert.equal(cameras.length,4,'fixture must have four cameras');
    for(const [camera] of cameras)assert(entry.text.includes(camera),`statistics missing ${camera}`);
    assert.match(entry.text,/960\s*×\s*540/,'statistics must display source video resolution');
    entry.verified={metadata:true,cameras:cameras.map(([key])=>key)};
  } else if(tab==='Filtering') {
    assert(!/No action data for velocity analysis/i.test(entry.text),'Filtering must recognize observation.action');
    await page.getByRole('heading',{name:/Action Velocity/}).waitFor();
    const distributions=await page.locator('svg[aria-label^="Δa distribution for "]').evaluateAll(nodes=>nodes.map(svg=>({
      dimension:svg.getAttribute('aria-label'),bars:[...svg.querySelectorAll('rect')].map(rect=>Number(rect.getAttribute('height'))),
    })));
    const actionFeature=dataset.info.features['observation.action'] || dataset.info.features.action;
    assert.equal(distributions.length,actionFeature.shape[0],'velocity must include each action dimension');
    assert(distributions.every(item=>item.bars.length>0 && item.bars.every(Number.isFinite) && item.bars.some(height=>height>0)),
      'velocity distributions must contain real finite histogram data');
    assert.match(entry.text,/σ=\d+\.\d+/,'velocity must include numeric standard deviation');
    entry.verified={velocityDimensions:distributions.length,distributions};
  } else if(tab==='Action Insights') {
    assert(!/^No action (columns found|data)/im.test(entry.text),'Action Insights must recognize observation.action');
    await page.getByRole('heading',{name:/Action Autocorrelation/}).waitFor();
    const curves=await page.locator('path.recharts-line-curve:visible').evaluateAll(nodes=>nodes.map(node=>node.getAttribute('d')));
    assert(curves.length>0,'Action Insights must display actual chart curves, not only axes or icons');
    assert(curves.every(curve=>curve && curve.length>30 && !/NaN|Infinity/.test(curve)),
      'action analysis curves must have real finite data');
    entry.verified={autocorrelation:true,chartCurves:curves.length,
      crossEpisodeVarianceUnavailable:dataset.info.total_episodes<2 && /Not enough episodes or no action data to compute variance/.test(entry.text)};
  } else if(tab==='Frames') {
    const cameras=Object.entries(dataset.info.features).filter(([,feature])=>feature.dtype==='video').map(([key])=>key);
    const selector=page.getByRole('combobox');
    assert.deepEqual(await selector.locator('option').evaluateAll(nodes=>nodes.map(node=>node.value)),cameras);
    const thumbnail=page.locator('video:visible').first();
    const read=()=>thumbnail.evaluate(video=>({src:video.currentSrc,currentTime:video.currentTime,duration:video.duration,
      width:video.videoWidth,height:video.videoHeight,readyState:video.readyState,seeking:video.seeking,
      decodedFrames:video.getVideoPlaybackQuality?.().totalVideoFrames,error:video.error?.message}));
    const waitReady=()=>page.waitForFunction(()=>[...document.querySelectorAll('video')].some(video=>
      video.getBoundingClientRect().width>0 && video.readyState>=2 && video.videoWidth>0 && !video.seeking),null,{timeout:45000});
    await waitReady();
    const first=await read();
    assert(first.width>0 && first.height>0 && first.decodedFrames>0 && !first.error,'thumbnail must decode a real frame');
    await page.getByRole('button',{name:'Toggle first/last frame',exact:true}).click();
    await page.waitForFunction(firstTime=>[...document.querySelectorAll('video')].some(video=>
      video.getBoundingClientRect().width>0 && video.readyState>=2 && !video.seeking && video.currentTime>firstTime+1),first.currentTime,{timeout:15000});
    const last=await read();
    assert(last.decodedFrames>first.decodedFrames,'last-frame selection must decode another video frame');
    assert(last.currentTime<=last.duration && last.currentTime>last.duration-0.2,'last-frame thumbnail must seek to the episode end');
    await page.screenshot({path:path.join(out,dataset.info.codebase_version+'-frames-last.png'),fullPage:true});
    await page.getByRole('button',{name:'Toggle first/last frame',exact:true}).click();
    await page.waitForFunction(firstTime=>[...document.querySelectorAll('video')].some(video=>
      video.getBoundingClientRect().width>0 && video.readyState>=2 && !video.seeking && Math.abs(video.currentTime-firstTime)<0.05),first.currentTime,{timeout:15000});
    entry.verified={cameras,first,last,restoredFirst:true};
  }
}

(async () => {
  const origin = process.env.DWP_TEST_ORIGIN || 'http://127.0.0.1:18095';
  const out = path.resolve(__dirname,'../.state/browser/features');
  fs.mkdirSync(out,{recursive:true});
  const token = execFileSync('ssh',['-o','BatchMode=yes','-o','ConnectTimeout=12','marvin',
    'cat /home/user/projects/data-web-platform/.state/access-token'],{encoding:'utf8'}).trim();
  const browser = await chromium.launch({headless:true,executablePath:process.env.DWP_TEST_BROWSER || undefined});
  const context = await browser.newContext({viewport:{width:1440,height:1000}});
  const page = await context.newPage();
  const resumeCheck = process.env.DWP_EXISTING_CHECK_JOB;
  const checkOnly = process.env.DWP_CHECK_ONLY === '1';
  assert(!checkOnly || resumeCheck,'DWP_CHECK_ONLY requires DWP_EXISTING_CHECK_JOB; never create another job accidentally');
  const result = {mode:checkOnly?'check-only':'full',errors:[],consoleErrors:[],failures:[],tabs:[],check:null};
  const summaryPath=path.join(out,checkOnly?'check-existing-summary.json':'summary.json');
  page.on('pageerror',error=>result.errors.push(String(error)));
  page.on('console',message=>{if(message.type()==='error')result.consoleErrors.push({text:message.text(),url:message.location().url});});
  page.on('response',response=>{if(response.status()>=400)result.failures.push({status:response.status(),url:response.url()});});
  try {
    await page.goto(origin,{waitUntil:'domcontentloaded'});
    await page.waitForURL(origin+'/login');
    await page.getByLabel('访问令牌',{exact:true}).fill(token);
    await page.getByRole('button',{name:'安全登录'}).click();
    await page.waitForURL(origin+'/');
    const datasetsResponse = await context.request.get(origin+'/api/datasets');
    assert(datasetsResponse.ok());
    const datasets = await datasetsResponse.json();
    for (const version of checkOnly ? [] : ['v2.1','v3.0']) {
      const dataset = datasets.find(item=>item.path.includes('/.state/verification-') && item.info.codebase_version===version);
      assert(dataset,version+' verification fixture');
      await page.goto(`${origin}/viewer/${dataset.repo_id}/0`,{waitUntil:'domcontentloaded'});
      await page.getByRole('button',{name:'Episodes',exact:true}).click();
      await page.locator('video').first().waitFor({state:'attached',timeout:45000});
      await page.getByText('preparing data & videos',{exact:true}).waitFor({state:'hidden',timeout:45000});
      for(const tab of ['Statistics','Filtering','Frames','Action Insights']) {
        const start = Date.now();
        await page.getByRole('button',{name:tab,exact:true}).click();
        // Allow the lazy tab to mount, then wait for its explicit loading UI.
        await page.waitForTimeout(300);
        await page.waitForFunction(() => !/Loading (cross-episode data|frames|statistics)|Computing episode statistics|preparing data & videos/.test(document.body.innerText),null,{timeout:45000});
        await page.waitForTimeout(300);
        const text = await page.locator('body').innerText();
        const entry = {version,repo_id:dataset.repo_id,tab,seconds:(Date.now()-start)/1000,
          text,svg:await page.locator('svg').count(),canvas:await page.locator('canvas').count(),
          images:await page.locator('img').evaluateAll(nodes=>nodes.map(image=>({src:image.currentSrc,width:image.naturalWidth,height:image.naturalHeight}))),
          warnings:text.split('\n').filter(line=>/no action|not enough|failed|error|no frames|no data|not available|unavailable/i.test(line))};
        result.tabs.push(entry);
        const filename=version+'-'+tab.toLowerCase().replaceAll(' ','-');
        fs.writeFileSync(path.join(out,filename+'.txt'),text);
        await page.screenshot({path:path.join(out,filename+'.png'),fullPage:true});
        try { await verifyTab(page,tab,dataset,entry,out); }
        catch(error) { entry.error=String(error); throw error; }
        console.log(JSON.stringify({version,tab,seconds:entry.seconds,svg:entry.svg,images:entry.images.length,warnings:entry.warnings,
          verified:{...entry.verified,distributions:undefined}}));
      }
    }
    await page.goto(origin,{waitUntil:'domcontentloaded'});
    await page.locator('#provider option[value=tianji]').waitFor({state:'attached'});
    let submitted;
    if(resumeCheck) {
      const existing=await context.request.get(origin+'/api/jobs/'+resumeCheck);
      assert(existing.ok());
      submitted=await existing.json();
      result.check={id:submitted.id,initialStatus:submitted.status,reused:true};
      await page.locator('.job-item').filter({hasText:resumeCheck.slice(0,12)}).click();
    } else {
    await page.locator('#source-root').fill('/media/user/BAG_STORAGE/recorded_bags');
    await page.locator('#load-recordings').click();
    await page.locator('#recordings input[type=checkbox]').first().waitFor();
    if(await page.locator('#select-none').isEnabled())await page.locator('#select-none').click();
    const recording='my_bag-26-09-16-15-56-37-27f8ef7d';
    await page.getByRole('checkbox',{name:'选择 '+recording,exact:true}).check();
    assert.equal(await page.locator('#recordings input[type=checkbox]:checked').count(),1);
    const responsePromise=page.waitForResponse(response=>new URL(response.url()).pathname==='/api/jobs' && response.request().method()==='POST');
    await page.getByRole('button',{name:'仅检查所选轨迹',exact:true}).click();
    const response=await responsePromise;
    assert(response.ok());
    submitted=await response.json();
    assert.equal(submitted.kind,'check');
    assert.equal(submitted.request.recordings.length,1);
    assert(submitted.request.recordings[0].endsWith('/'+recording));
    result.check={id:submitted.id,initialStatus:submitted.status,sawActive:false};
    await page.waitForFunction(()=>/运行中|排队中/.test(document.querySelector('#job-status')?.textContent || ''),null,{timeout:15000});
    result.check.sawActive=true;
    await page.screenshot({path:path.join(out,'anomaly-check-running.png'),fullPage:true});
    }
    await page.waitForFunction(()=>document.querySelector('#check-results .check-item') && /异常/.test(document.querySelector('#check-results')?.textContent || ''),null,{timeout:90000});
    const details=page.locator('#check-results .check-item').first();
    if(await details.getAttribute('open')===null)await details.locator(':scope > summary').click();
    result.check.text=await page.locator('#job-detail').innerText();
    const finished=await context.request.get(origin+'/api/jobs/'+submitted.id);
    result.check.job=await finished.json();
    assert.equal(result.check.job.status,'failed');
    assert.equal(result.check.job.kind,'check');
    assert.equal(result.check.job.check_results.length,1);
    assert.equal(result.check.job.check_results[0].valid,false);
    assert.match(result.check.text,/my_bag-26-09-16-15-56-37-27f8ef7d/,'expanded UI result must identify the selected bad recording');
    assert.match(result.check.text,/异常|缺口|gap/i,'expanded UI must expose the anomaly, not only a failed status');
    await page.screenshot({path:path.join(out,'anomaly-check-result.png'),fullPage:true});
    console.log(JSON.stringify({check:result.check}));
    assert.deepEqual(result.errors,[]);
    assert.deepEqual(result.consoleErrors,[]);
    assert.deepEqual(result.failures,[]);
  } finally {
    fs.writeFileSync(summaryPath,JSON.stringify(result,null,2));
    await browser.close();
  }
})().catch(error=>{console.error(error);process.exitCode=1;});
