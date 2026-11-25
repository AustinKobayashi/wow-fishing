const assert = require('node:assert/strict')
const fs = require('node:fs')
const path = require('node:path')
const vm = require('node:vm')
const {test} = require('node:test')

function preview() {
 const elements = Object.fromEntries(['activityBox', 'activityName', 'activityTimer', 'activityCaption',
  'activityDetail', 'activityNext', 'activityFill', 'audioLevel', 'audioLive', 'audioStatus', 'audioScale',
  'audioThresholdValue', 'audioThreshold', 'audioLine', 'audioArea', 'audioDot'].map(id => [id,
  {textContent:'', style:{}, dataset:{}, attributes:{}, setAttribute(name, value) { this.attributes[name] = value }}]))
 const sources = [], listeners = {}, timers = new Set()
 let now = 0
 class EventSource {
  constructor(url) { assert.equal(url, '/api/stages')
   sources.push(this) }
  addEventListener(type, callback) { assert.equal(type, 'stage')
   this.message = callback }
  close() { this.closed = true }
 }
 vm.runInNewContext(fs.readFileSync(path.join(__dirname, '../web/activity.js'), 'utf8'), {
  document:{getElementById:id => { assert.ok(elements[id], id)
   return elements[id] }},
  performance:{now:() => now}, EventSource,
  setInterval:(callback, interval) => { assert.equal(interval, 100)
   timers.add(callback)
   return callback }, clearInterval:callback => timers.delete(callback),
  window:{addEventListener:(event, callback) => listeners[event] = callback}
 })
 return {elements, sources, listeners, timers,
  send:(name, duration, elapsed=0, id=1, extra={}) => sources.at(-1).message({data:JSON.stringify({name,duration,elapsed,id,...extra})}),
  advance:milliseconds => { now += milliseconds
   timers.forEach(callback => callback()) }}
}

test('uses sampled timing, resumes elapsed progress and never advances a stage at zero', () => {
 const p = preview(), e = p.elements
 p.send('reaction', 2, .5)
 assert.equal(e.activityName.textContent, 'Waiting reaction delay')
 assert.equal(e.activityTimer.textContent, '1.5 s remaining')
 assert.equal(e.activityFill.style.width, '75%')
 p.advance(1000)
 assert.equal(e.activityTimer.textContent, '0.5 s remaining')
 assert.equal(e.activityFill.style.width, '25%')
 p.advance(1000)
 assert.equal(e.activityTimer.textContent, '0.0 s remaining')
 assert.equal(e.activityName.textContent, 'Waiting reaction delay')
 assert.equal(e.activityFill.style.width, '0%')
 p.send('reeling', .2, 0, 2)
 assert.equal(e.activityName.textContent, 'Pressing reel key')
 p.send('reaction', 2, 0, 1)
 assert.equal(e.activityName.textContent, 'Pressing reel key')
})

test('listening shows elapsed time and recast deadline rather than a predicted bite', () => {
 const p = preview(), e = p.elements
 p.send('listening', 59.8, 12)
 assert.equal(e.activityTimer.textContent, '12.0 s listening')
 assert.equal(e.activityCaption.textContent, 'Recast in 47.8 s')
 p.send('listening', 59.8, 60)
 assert.equal(e.activityCaption.textContent, 'Recast due. Waiting for bot.')
 assert.equal(e.activityName.textContent, 'Waiting for bite audio')
 assert.equal(e.activityFill.style.width, '0%')
})

test('handles branches, stop, error and untimed stages', () => {
 const p = preview(), e = p.elements
 let id = 0
 for (const [name, label] of [['casting','Casting'], ['skipping','Waiting out fishing window'],
  ['idle','Taking idle break'], ['castdelay','Waiting before next cast']]) {
  p.send(name, 3, 1, ++id)
  assert.equal(e.activityName.textContent, label)
  assert.equal(e.activityTimer.textContent, '2.0 s remaining')
 }
 for (const [name, label] of [['starting','Starting'], ['paused','Paused'], ['stopping','Stopping'], ['stopped','Stopped'], ['error','Session error']]) {
  p.send(name, null, 0, ++id)
  assert.equal(e.activityName.textContent, label)
  assert.equal(e.activityFill.style.width, '0%')
  assert.equal(e.activityCaption.textContent, 'No active countdown')
  if (name === 'paused') {
   assert.equal(e.activityTimer.textContent, 'Timer paused')
   assert.match(e.activityNext.textContent, /Cast a line to resume/)
  }
 }
})

test('lost or stale updates suspend timing and reconnect accepts a restarted server', () => {
 const p = preview(), e = p.elements
 p.send('idle', 12, 1, 20)
 p.sources[0].onerror()
 assert.equal(e.activityName.textContent, 'State unavailable')
 assert.equal(e.activityTimer.textContent, 'Unknown')
 p.advance(1000)
 assert.equal(e.activityFill.style.width, '0%')
 p.send('casting', .2, 0, 1)
 assert.equal(e.activityName.textContent, 'Casting')
 p.advance(6100)
 assert.equal(e.activityName.textContent, 'State unavailable')
 p.send('listening', 60, 13, 2)
 assert.equal(e.activityTimer.textContent, '13.0 s listening')
 p.listeners.pagehide()
 assert.equal(p.sources[0].closed, true)
 assert.equal(p.timers.size, 0)
 p.listeners.pageshow({persisted:true})
 assert.equal(p.sources.length, 2)
 assert.equal(p.timers.size, 1)
 p.send('idle', 15, 5, 3)
 assert.equal(e.activityTimer.textContent, '10.0 s remaining')
})

test('audio uses the saved threshold and retains only one second of distinct readings', () => {
 const p = preview(), e = p.elements
 const send = (sampled, level, age=0) => p.send('listening', 60, 0, 1, {threshold:7000, audio:{sampled,level,age}})
 send(1, 8000)
 assert.equal(e.audioLevel.textContent, '')
 p.advance(100)
 assert.equal(e.audioLevel.textContent, '8,000')
 assert.equal(e.audioStatus.textContent, 'Above threshold')
 assert.equal(e.audioThresholdValue.textContent, '7,000')
 assert.equal(e.audioThreshold.attributes.visibility, 'visible')
 send(1, 8000, .1)
 p.advance(100)
 assert.equal((e.audioLine.attributes.d.match(/[ML]/g) || []).length, 1)
 send(2, 7000)
 p.advance(100)
 assert.equal(e.audioStatus.textContent, 'Below threshold')
 assert.equal((e.audioLine.attributes.d.match(/[ML]/g) || []).length, 2)
 for (let i = 3; i < 18; i++) {
  send(i, 1500)
  p.advance(100)
 }
 assert.ok((e.audioLine.attributes.d.match(/[ML]/g) || []).length <= 11)
 p.advance(1100)
 assert.equal(e.audioLevel.textContent, '--')
 assert.equal(e.audioStatus.textContent, 'Waiting for an audio reading')
 assert.equal(e.audioLine.attributes.d, '')
 assert.equal(e.audioDot.attributes.visibility, 'hidden')
})

test('audio preserves a detection briefly but does not label inactive or disconnected input live', () => {
 const p = preview(), e = p.elements
 const extra = {threshold:7000, audio:{sampled:1,level:9000,age:0}}
 p.send('reaction', 1.2, 0, 2, extra)
 p.advance(100)
 assert.equal(e.audioLevel.textContent, '--')
 assert.equal(e.audioLive.textContent, 'INACTIVE')
 assert.match(e.audioLine.attributes.d, /^M/)
 p.sources[0].onerror()
 assert.equal(e.audioLine.attributes.d, '')
 assert.equal(e.audioLive.textContent, 'OFFLINE')
 p.send('listening', 60, 0, 1, extra)
 p.advance(100)
 assert.equal(e.audioLevel.textContent, '9,000')
 p.listeners.pagehide()
 p.listeners.pageshow({persisted:true})
 assert.equal(e.audioLine.attributes.d, '')
 assert.equal(e.audioLevel.textContent, '--')
})

test('audio shows silence as zero, rejects invalid readings, and leaves measurement gaps blank', () => {
 const p = preview(), e = p.elements
 p.send('listening', 60, 0, 1, {threshold:0, audio:{sampled:1,level:0,age:0}})
 p.advance(100)
 assert.equal(e.audioLevel.textContent, '0')
 assert.equal(e.audioStatus.textContent, 'Below threshold')
 p.advance(300)
 p.send('listening', 60, 0, 1, {threshold:0, audio:{sampled:2,level:100,age:0}})
 p.advance(100)
 assert.equal((e.audioLine.attributes.d.match(/M/g) || []).length, 2)
 for (const audio of [null, {level:40000,sampled:3,age:0}, {level:100,sampled:3,age:-1}, {level:'100',sampled:3,age:0}]) {
  p.send('listening', 60, 0, 1, {threshold:7000,audio})
  p.advance(100)
  assert.equal(e.audioLevel.textContent, '--')
  assert.doesNotMatch(e.audioLine.attributes.d, /NaN|Infinity/)
 }
})
