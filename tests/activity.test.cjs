const assert = require('node:assert/strict')
const fs = require('node:fs')
const path = require('node:path')
const vm = require('node:vm')
const {test} = require('node:test')

function preview() {
 const elements = Object.fromEntries(['activityBox', 'activityName', 'activityTimer', 'activityCaption',
  'activityDetail', 'activityNext', 'activityFill'].map(id => [id, {textContent:'', style:{}, dataset:{}}]))
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
  setInterval:callback => { timers.add(callback)
   return callback }, clearInterval:callback => timers.delete(callback),
  window:{addEventListener:(event, callback) => listeners[event] = callback}
 })
 return {elements, sources, listeners, timers,
  send:(name, duration, elapsed=0, id=1) => sources.at(-1).message({data:JSON.stringify({name,duration,elapsed,id})}),
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
 for (const [name, label] of [['starting','Starting'], ['stopping','Stopping'], ['stopped','Stopped'], ['error','Session error']]) {
  p.send(name, null, 0, ++id)
  assert.equal(e.activityName.textContent, label)
  assert.equal(e.activityFill.style.width, '0%')
  assert.equal(e.activityCaption.textContent, 'No active countdown')
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
