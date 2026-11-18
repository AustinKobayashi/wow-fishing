const assert = require('node:assert/strict')
const fs = require('node:fs')
const path = require('node:path')
const vm = require('node:vm')
const {test} = require('node:test')

test('microphone controls and polling survive restart without losing drafts', async () => {
 const html = fs.readFileSync(path.join(__dirname, '../web/index.html'), 'utf8')
 const elements = Object.fromEntries([...html.matchAll(/id="([^"]+)"/g)].map(([, id]) =>
  [id, {value:'', textContent:'', classList:{add() {}, remove() {}, toggle() {}}}]))
 const context = vm.createContext({
  document:{getElementById:id => elements[id], querySelectorAll:() => []},
  window:{addEventListener() {}}, AbortController,
  fetch:() => new Promise(() => {}), setTimeout() {}, clearTimeout() {}
 })
 vm.runInContext(fs.readFileSync(path.join(__dirname, '../web/app.js'), 'utf8'), context)
 vm.runInContext('connected = true\npreviewValid = true\ncurrentState = {active:false, status:"Stopped"}', context)
 context.controls()
 assert.equal(elements.reconnectMicrophone.disabled, false)
 for (const state of [{active:true, status:'Running'}, {active:true, status:'Stopping'},
                      {active:false, status:'Restarting'}]) {
  context.nextState = state
  vm.runInContext('currentState = nextState', context)
  context.controls()
  assert.equal(elements.reconnectMicrophone.disabled, true)
  assert.equal(elements.start.disabled, true)
 }
 let request
 context.action = (url, payload) => { request = {url, payload} }
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

 vm.runInContext('currentState = {active:false, status:"Restarting"}', context)
 context.api = async () => { throw Error('Restart in progress') }
 await context.poll()
 assert.equal(elements.reconnectMicrophone.disabled, true)
 assert.match(elements.notice.textContent, /Fishing stays stopped/)
})
