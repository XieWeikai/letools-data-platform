const test = require('node:test');
const assert = require('node:assert/strict');
const T = require('../src/data_web_platform/static/raw-timeline.js');

test('camera offset and missing ranges stay on the shared recording timeline',()=>{
  const video={offset:1,end:1.8,times:[1,1.033,1.066,1.8]};
  assert.equal(T.frameAt(video,.999).outside,true);
  assert.equal(T.frameAt(video,1.04).index,1);
  assert.equal(T.frameAt(video,1.7).index,2);
  assert.ok(T.frameAt(video,1.7).age>.6);
  assert.equal(T.frameAt(video,1.801).outside,true);
});
test('frame buttons follow real variable timestamps rather than assuming 50 FPS',()=>{
  const video={times:[1,1.033,1.066,1.8]};
  assert.equal(T.step(video,1.066,1,2),1.8);
  assert.equal(T.step(video,1.8,-1,2),1.066);
  assert.equal(T.step(video,1.7,-1,2),1.066);
});
test('curves break across missing observations and non-finite values',()=>{
  const path=T.paths([0,.01,.5,.51,.52],[[1],[2],[3],[null],[4]],0,.1,x=>x,y=>y);
  assert.equal((path.match(/M/g)||[]).length,3);
  assert.equal((path.match(/L/g)||[]).length,1);
});
test('display reduction does not invent gaps between retained extrema',()=>{
  const path=T.paths([0,.5,1,2],[[1],[2],[3],[4]],0,.1,x=>x,y=>y,[true,false,false,true]);
  assert.equal((path.match(/M/g)||[]).length,2);
  assert.equal((path.match(/L/g)||[]).length,2);
});
