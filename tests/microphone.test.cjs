const assert = require('node:assert/strict')
const fs = require('node:fs')
const path = require('node:path')
const vm = require('node:vm')
const {test} = require('node:test')

test('session controls support resume and polling survives restart without losing drafts', async () => {
 const html = fs.readFileSync(path.join(__dirname, '../web/index.html'), 'utf8')
 const elements = Object.fromEntries([...html.matchAll(/id="([^"]+)"/g)].map(([, id]) =>
  [id, {value:'', textContent:'', classList:{add() {}, remove() {}, toggle() {}}}]))
 assert.equal(elements.pause, undefined)
 const context = vm.createContext({
  document:{getElementById:id => elements[id], querySelectorAll:() => []},
  window:{addEventListener() {}}, AbortController,
  fetch:() => new Promise(() => {}), setTimeout() {}, clearTimeout() {}
 })
 vm.runInContext(fs.readFileSync(path.join(__dirname, '../web/app.js'), 'utf8'), context)
 vm.runInContext('connected = true\npreviewValid = true\ncurrentState = {active:false, status:"Stopped"}', context)
 context.controls()
 assert.equal(elements.reconnectMicrophone.disabled, false)
 assert.equal(elements.startLabel.textContent, 'Cast a line')
 assert.equal(elements.startHint.textContent, 'START BOT')
 assert.equal(elements.stop.textContent, 'Pause fishing')
 assert.equal(elements.stop.disabled, true)
 let request
 context.action = (url, payload) => { request = {url, payload} }
 for (const state of [{active:true, status:'Starting'}, {active:true, status:'Running'}, {active:true, status:'Stopping'},
                      {active:false, status:'Restarting'}]) {
  context.nextState = state
  vm.runInContext('currentState = nextState', context)
  context.controls()
  assert.equal(elements.reconnectMicrophone.disabled, true)
  assert.equal(elements.start.disabled, true)
  const canPause = ['Starting', 'Running'].includes(state.status)
  assert.equal(elements.stop.disabled, !canPause)
  if (canPause) {
   assert.equal(elements.stop.textContent, 'Pause fishing')
   elements.stop.onclick()
   assert.equal(request.url, '/api/pause')
  }
 }
 context.applyState({active:true, status:'Paused', error:null, elapsed:65,
  config:{THRESHOLD:7000, DONT_REEL_TIME:2.1, MAX_RUN_TIME:3600}, logs:[]})
 assert.equal(elements.start.disabled, false)
 assert.equal(elements.startLabel.textContent, 'Resume fishing')
 assert.equal(elements.startHint.textContent, 'RESUME BOT')
 assert.equal(elements.stop.disabled, false)
 assert.equal(elements.stop.textContent, 'Stop fishing')
 assert.equal(elements.save.disabled, true)
 assert.equal(elements.reset.disabled, true)
 assert.equal(elements.reconnectMicrophone.disabled, true)
 assert.equal(elements.elapsed.textContent, '00:01:05')
 assert.match(elements.notice.textContent, /timer is paused/)
 assert.doesNotMatch(elements.notice.textContent, /Five consecutive casts failed/)
 vm.runInContext('dirty = true\npreviewValid = false', context)
 context.controls()
 assert.equal(elements.start.disabled, false)
 vm.runInContext('connected = false', context)
 context.controls()
 assert.equal(elements.start.disabled, true)
 vm.runInContext('connected = true\ndirty = false\npreviewValid = true', context)
 elements.start.onclick()
 assert.equal(request.url, '/api/start')
 elements.stop.onclick()
 assert.equal(request.url, '/api/stop')
 vm.runInContext('currentState = {active:true, status:"Running"}', context)
 context.controls()
 assert.equal(elements.stop.textContent, 'Pause fishing')
 elements.stop.onclick()
 assert.equal(request.url, '/api/pause')
 elements.reconnectMicrophone.onclick()
 assert.equal(request.url, '/api/reconnect-microphone')
 assert.deepEqual(Object.keys(request.payload), [])

 vm.runInContext('token = "old"\ndirty = true\ncfg = {THRESHOLD:123}\nhiddenThrough = 99\nentries = []', context)
 context.api = async url => {
  assert.equal(url, '/api/bootstrap')
  return {token:'new', state:{active:false, status:'Stopped', error:null, elapsed:0,
   config:{THRESHOLD:7000, DONT_REEL_TIME:2.1, MAX_RUN_TIME:3600}, logs:[],
   microphone:'Microphone ready. Fishing is stopped.'}}
 }
 await context.poll()
 assert.equal(vm.runInContext('token', context), 'new')
 assert.equal(vm.runInContext('hiddenThrough', context), 0)
 assert.equal(vm.runInContext('dirty && cfg.THRESHOLD === 123', context), true)
 assert.equal(elements.microphoneMessage.textContent, 'Microphone ready. Fishing is stopped.')
 assert.equal(elements.reconnectMicrophone.disabled, false)
 assert.equal(elements.start.disabled, true)
 assert.equal(elements.startLabel.textContent, 'Cast a line')

 vm.runInContext('currentState = {active:false, status:"Restarting"}', context)
 context.api = async () => { throw Error('Restart in progress') }
 await context.poll()
 assert.equal(elements.reconnectMicrophone.disabled, true)
 assert.match(elements.notice.textContent, /Fishing stays stopped/)
})
