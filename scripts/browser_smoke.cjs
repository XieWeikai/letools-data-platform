/* Browser smoke through an SSH tunnel. Never logs or persists the login token. */
const { execFileSync } = require('node:child_process');
const fs = require('node:fs');
const path = require('node:path');
const assert = require('node:assert/strict');
const { chromium } = require(process.env.PLAYWRIGHT_MODULE || 'playwright');

const quoteShell = value => "'" + String(value).replaceAll("'", "'\\''") + "'";
const atomShape = atom => ({role:atom.role, content:atom.content ?? null, style:atom.style ?? null,
  timestamp:atom.timestamp, camera:atom.camera ?? null, tool_calls:atom.tool_calls ?? null});
const readMedia = page => page.locator('video').evaluateAll(nodes => nodes.map(video => ({
  src:video.currentSrc, readyState:video.readyState, currentTime:video.currentTime,
  duration:video.duration, width:video.videoWidth, height:video.videoHeight,
  paused:video.paused, decodedFrames:video.getVideoPlaybackQuality?.().totalVideoFrames,
  error:video.error?.message,
})));

(async () => {
  const origin = process.env.DWP_TEST_ORIGIN || 'http://127.0.0.1:18095';
  const out = path.resolve(__dirname, '../.state/browser');
  fs.mkdirSync(out, {recursive: true});
  const token = execFileSync('ssh', ['-o', 'BatchMode=yes', '-o', 'ConnectTimeout=12', 'marvin',
    'cat /home/user/projects/data-web-platform/.state/access-token'], {encoding: 'utf8'}).trim();
  const browser = await chromium.launch({headless: true, executablePath:process.env.DWP_TEST_BROWSER || undefined});
  const context = await browser.newContext({viewport: {width: 1440, height: 1000}});
  const page = await context.newPage();
  const errors = [], consoleErrors = [], failures = [], views = [];
  page.on('pageerror', e => errors.push(String(e)));
  page.on('console', message => { if (message.type() === 'error') consoleErrors.push({text:message.text(),url:message.location().url}); });
  page.on('response', response => { if (response.status() >= 400) failures.push({status:response.status(), url:response.url()}); });
  async function jsonRequest(method, endpoint, data) {
    const response = await context.request[method](origin + endpoint, {data, timeout:120000});
    assert(response.ok(), `${method.toUpperCase()} ${endpoint}: ${response.status()} ${await response.text()}`);
    return response.json();
  }
  async function pauseVideos() {
    const pause = page.locator('button[title="Pause. Toggle with Space"]');
    if (await pause.count()) await pause.click();
    await page.waitForFunction(() => [...document.querySelectorAll('video')].every(video => video.paused));
  }
  async function testPlayback(view) {
    await page.locator('video').first().waitFor({state:'attached',timeout:45000});
    await page.waitForFunction(() => {
      const media = [...document.querySelectorAll('video')];
      return media.length === 4 && media.every(video => video.readyState >= 2 && video.videoWidth > 0);
    },null,{timeout:45000});
    await page.getByText('preparing data & videos', {exact:true}).waitFor({state:'hidden',timeout:45000});
    await pauseVideos();
    const before = await readMedia(page);
    await page.getByTitle('Play. Toggle with Space', {exact:true}).click();
    await page.waitForFunction(startTimes => [...document.querySelectorAll('video')].every((video,index) =>
      video.currentTime >= startTimes[index] + 1 && !video.paused), before.map(video=>video.currentTime), {timeout:15000});
    await pauseVideos();
    const after = await readMedia(page);
    after.forEach((media,index) => {
      assert(media.currentTime > before[index].currentTime + 0.5, 'video clock must advance');
      assert(media.decodedFrames > before[index].decodedFrames, 'video must decode additional frames');
      assert(!media.error, media.error);
    });
    // Test rewind from a meaningful distance after playback. Upstream ignores
    // seeks smaller than VIDEO_SYNC_TOLERANCE (0.2s), so testing at 0.1s would
    // assert a stronger frame-accurate contract than this player provides.
    await page.getByTitle('Rewind from start', {exact:true}).click();
    await page.waitForFunction(() => [...document.querySelectorAll('video')].every(video =>
      video.paused && Math.abs(video.currentTime) < 0.1), null, {timeout:15000});
    const rewind = await readMedia(page);
    const slider = page.getByRole('slider',{name:'Seek video',exact:true});
    await slider.scrollIntoViewIfNeeded();
    const box = await slider.boundingBox();
    assert(box && box.width > 0, 'seek slider must be visible');
    // A real pointer seek through the UI; do not manipulate video.currentTime.
    await page.mouse.click(box.x + box.width * 0.5, box.y + box.height * 0.5);
    await page.waitForFunction(() => [...document.querySelectorAll('video')].every(video =>
      video.paused && Math.abs(video.currentTime - video.duration * 0.5) < 0.35), null, {timeout:15000});
    view.playback = {before, after, rewind, seek:await readMedia(page)};
    await page.screenshot({path:path.join(out,`viewer-${view.version}.png`),fullPage:true});
  }
  async function testAnnotations(dataset, view) {
    assert(dataset.path.includes('/.state/verification-'), 'annotation writes require an isolated verification dataset');
    const ref = {repo_id:dataset.repo_id};
    const endpoint = '/api/episodes/0/atoms';
    const query = '?repo_id=' + encodeURIComponent(dataset.repo_id);
    await jsonRequest('post','/api/dataset/load',ref);
    const original = (await jsonRequest('get',endpoint+query)).atoms;
    fs.writeFileSync(path.join(out,`annotations-original-${view.version}.json`), JSON.stringify({repo_id:dataset.repo_id,atoms:original},null,2));
    const marker = `Browser verification ${view.version} ${Date.now()}`;
    view.annotations = {originalAtoms:original.length, restored:false};
    try {
      await page.getByRole('button',{name:'Annotations',exact:true}).click();
      await page.getByText('Language annotations',{exact:true}).waitFor();
      await page.locator('.quick-add select').selectOption('task_aug');
      await page.getByPlaceholder('pick up the blue cube and place it in the green box',{exact:true}).fill(marker);
      await page.getByRole('button',{name:'+ Add at frame',exact:true}).click();
      const savedResponse = page.waitForResponse(response => new URL(response.url()).pathname === endpoint && response.request().method() === 'POST');
      await page.getByRole('button',{name:'Save episode',exact:true}).click();
      const saved = await savedResponse;
      assert(saved.ok(), `Annotation UI save returned ${saved.status()}: ${await saved.text()}`);
      await page.getByText(/^Saved episode to /).waitFor();
      const stored = (await jsonRequest('get',endpoint+query)).atoms;
      assert.equal(stored.length,original.length+1);
      assert(stored.some(atom=>atom.content===marker && atom.style==='task_aug' && atom.timestamp===0));
      await page.screenshot({path:path.join(out,`annotations-${view.version}.png`),fullPage:true});
      await page.reload({waitUntil:'domcontentloaded'});
      await page.getByRole('button',{name:'Annotations',exact:true}).click();
      await page.getByText(marker,{exact:true}).first().waitFor({timeout:45000});
      view.annotations.savedAndReloaded = true;

      // The default export destination is a fresh, server-owned .state/exports
      // directory. No Hub endpoint is used, and originals are never overwritten.
      const exported = await jsonRequest('post','/api/export',{...ref,copy_videos:false});
      assert(exported.output_dir.includes('/.state/exports/'), 'export must stay in the private test state directory');
      assert(exported.persistent_rows > 0, 'annotation must be materialized into exported frame rows');
      const verifyExport = `import json, pathlib, sys
import pyarrow.parquet as pq
p = pathlib.Path(sys.argv[1])
marker = sys.argv[2]
info = json.loads((p / "meta/info.json").read_text())
ann = json.loads((p / "meta/lerobot_annotations.json").read_text())
data = list((p / "data").rglob("*.parquet"))
videos = list((p / "videos").rglob("*.mp4"))
assert data and len(videos) == 4
assert "language_persistent" in info["features"] and "language_events" in info["features"]
assert any(a["content"] == marker for a in ann["episodes"]["0"]["atoms"])
tables = [pq.read_table(f, columns=["language_persistent", "language_events"]) for f in data]
frame_rows = sum(table.num_rows for table in tables)
annotated_rows = sum(marker in json.dumps(value) for table in tables for value in table["language_persistent"].to_pylist())
assert frame_rows == info["total_frames"] and annotated_rows > 0
print(json.dumps({"path": str(p), "version": info["codebase_version"], "dataFiles": len(data), "videoFiles": len(videos), "frameRows": frame_rows, "annotatedRows": annotated_rows, "annotationPresent": True}))`;
      const environment = await jsonRequest('get','/api/environment');
      const disk = JSON.parse(execFileSync('ssh',['-o','BatchMode=yes','-o','ConnectTimeout=12','marvin',
        `${quoteShell(environment.python)} -c ${quoteShell(verifyExport)} ${quoteShell(exported.output_dir)} ${quoteShell(marker)}`],{encoding:'utf8',timeout:30000}));
      const collision = await context.request.post(origin+'/api/export',{data:{...ref,output_dir:exported.output_dir,copy_videos:false}});
      assert.equal(collision.status(),409,'export must reject an existing destination');
      view.export = {...exported,disk,overwriteRejected:true};
    } finally {
      await jsonRequest('post',endpoint,{...ref,episode_index:0,atoms:original});
      const restored = (await jsonRequest('get',endpoint+query)).atoms;
      assert.deepEqual(restored.map(atomShape),original.map(atomShape),'test must restore the original annotation content');
      view.annotations.restored = true;
    }
  }
  try {
    await page.goto(origin, {waitUntil: 'domcontentloaded'});
    await page.waitForURL(origin+'/login');
    await page.getByLabel('访问令牌', {exact:true}).fill(token);
    await page.getByRole('button', {name: '安全登录'}).click();
    await page.waitForURL(origin + '/');
    await page.getByRole('heading', {name:'数据处理工作台',exact:true}).waitFor();
    await page.locator('#provider option[value=tianji]').waitFor({state:'attached'});
    assert.equal(await page.locator('#provider').inputValue(), 'tianji');
    await page.screenshot({path:path.join(out,'workbench-desktop.png'),fullPage:true});
    await page.locator('[data-browse="source-root"]').click();
    await page.locator('#folder-dialog').waitFor({state:'visible'});
    await page.keyboard.press('Escape');
    await page.locator('#folder-dialog').waitFor({state:'hidden'});
    await page.locator('#source-root').fill('/media/user/BAG_STORAGE/recorded_bags');
    await page.locator('#load-recordings').click();
    await page.locator('#recordings input[type=checkbox]').first().waitFor();
    await page.locator('#select-all').click();
    assert((await page.locator('#recordings input[type=checkbox]:checked').count()) > 1);
    await page.locator('#select-none').click();
    assert.equal(await page.locator('#recordings input[type=checkbox]:checked').count(), 0);
    await page.locator('#recordings input[type=checkbox]').first().check();
    assert.equal(await page.locator('#recordings input[type=checkbox]:checked').count(), 1);
    await page.screenshot({path:path.join(out,'recordings-desktop.png'),fullPage:true});
    await page.setViewportSize({width:375,height:812});
    await page.screenshot({path:path.join(out,'workbench-mobile.png'),fullPage:true});
    assert(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth + 1), 'mobile horizontal overflow');
    await page.setViewportSize({width:1440,height:1000});
    await page.locator('[data-view=datasets]').click();
    await page.locator('.dataset-card').first().waitFor();
    await page.screenshot({path:path.join(out,'datasets.png'),fullPage:true});
    const datasetsResponse = await context.request.get(origin + '/api/datasets');
    assert(datasetsResponse.ok());
    const datasets = await datasetsResponse.json();
    for (const dataset of datasets) {
      if (!dataset.path.includes('/.state/verification-')) continue;
      const view = {version:dataset.info.codebase_version,repo_id:dataset.repo_id,passed:false};
      views.push(view);
      try {
        await page.goto(`${origin}/viewer/${dataset.repo_id}/0`, {waitUntil:'domcontentloaded',timeout:45000});
        await testPlayback(view);
        await testAnnotations(dataset,view);
        await page.getByRole('button',{name:'Doctor',exact:true}).click();
        await page.screenshot({path:path.join(out,`viewer-doctor-${view.version}.png`),fullPage:true});
        assert(/Doctor/i.test(await page.locator('body').innerText()));
        await page.goto(`${origin}/doctor?dataset=${encodeURIComponent(dataset.repo_id)}`,{waitUntil:'domcontentloaded'});
        await page.getByRole('heading',{name:'LeRobot Doctor',exact:true}).waitFor();
        assert.equal(await page.locator('input[name=dataset]').inputValue(),dataset.repo_id);
        assert.equal(await page.locator('select[name=operation] option').count(),6);
        await page.screenshot({path:path.join(out,`doctor-${view.version}.png`),fullPage:true});
        view.doctor = {viewerTab:true,platformPage:true,operations:6};
        view.passed = true;
      } catch(error) {
        view.error = String(error);
        view.failureMedia = await readMedia(page);
        await page.screenshot({path:path.join(out,`failure-${view.version}.png`),fullPage:true});
      }
      console.log(JSON.stringify(view));
    }
    fs.writeFileSync(path.join(out,'summary.json'),JSON.stringify({errors,consoleErrors,failures,views},null,2));
    console.log(JSON.stringify({errors,consoleErrors,failures,screenshots:out}));
    assert.deepEqual(errors,[]);
    assert.deepEqual(new Set(views.map(view=>view.version)),new Set(['v2.1','v3.0']));
    assert(views.every(view=>view.passed), 'every verification dataset must pass actual playback, seek, annotation and export');
    // Known during the first build only: optional sarm_progress/srm_progress
    // probes returned 403. Keep them visible and fail until the overlay is fixed;
    // never hide arbitrary HTTP failures or console errors.
    assert.deepEqual(failures,[]);
    assert.deepEqual(consoleErrors,[]);
  } finally {
    fs.writeFileSync(path.join(out,'summary.json'),JSON.stringify({errors,consoleErrors,failures,views},null,2));
    await page.screenshot({path:path.join(out,'last-state.png'),fullPage:true}).catch(()=>{});
    await browser.close();
  }
})().catch(error=>{console.error(error);process.exitCode=1;});
