const assert = require('node:assert/strict')
const fs = require('node:fs')
const path = require('node:path')
const vm = require('node:vm')
const {test} = require('node:test')

function page() {
 function element() {
  return {value:'', textContent:'', children:[], classList:{toggle() {}},
   append(...children) { this.children.push(...children) },
   prepend(child) { this.children.unshift(child) },
   replaceChildren() { this.children = [] },
   setAttribute(name, value) { this[name] = value }}
 }
 const html = fs.readFileSync(path.join(__dirname, '../web/index.html'), 'utf8')
 const elements = Object.fromEntries([...html.matchAll(/id="([^"]+)"/g)].map(([, id]) => [id, element()]))
 const context = vm.createContext({
  document:{getElementById:id => { assert.ok(elements[id], id)
   return elements[id] }, createElement:element, querySelectorAll:() => []},
  window:{addEventListener() {}}, AbortController,
  fetch:() => new Promise(() => {}), setTimeout() {}, clearTimeout() {}
 })
 vm.runInContext(fs.readFileSync(path.join(__dirname, '../web/app.js'), 'utf8'), context)
 return {elements, html, apply:data => context.applyCatches(data), edit:entry => context.catchRowEditor(entry)}
}

const emptyRates = {seconds:0, amount:0, perHour:null, stacks:0, stacksPerHour:null, catches:[]}
const session = (location, sessionLength, catches=[]) => ({location, sessionLength, catches, date:'2026-11-08T12:00:00Z'})
const rates = (fish, amount) => ({seconds:3600, amount, perHour:amount, stacks:1, stacksPerHour:1,
 catches:[{fish, amount, perHour:amount, stacks:1, nonStackingAmount:0}]})

test('catch suggestions come from saved data on a fresh page and refresh with the logs', () => {
 const p = page(), e = p.elements
 p.apply({sessions:[session('Stormwind', 3600), session('Stormwind', 3600), session('Goldshire', 3600)],
  fishNames:['Catfish', 'Goldfish'], rates:emptyRates,
  ratesByLocation:{Stormwind:emptyRates, Goldshire:emptyRates}})
 assert.deepEqual(e.catchLocations.children.map(option => option.value), ['Goldshire', 'Stormwind'])
 assert.deepEqual(e.fishNames.children.map(option => option.value), ['Catfish', 'Goldfish'])
 e.catchLocation.value = 'Unsaved location'
 e.fishName.value = 'Unsaved fish'
 p.apply({sessions:[session('Booty Bay', 3600)], fishNames:['Tuna'], rates:emptyRates,
  ratesByLocation:{'Booty Bay':emptyRates}})
 assert.deepEqual(e.catchLocations.children.map(option => option.value), ['Booty Bay'])
 assert.deepEqual(e.fishNames.children.map(option => option.value), ['Tuna'])
 assert.equal(e.catchLocation.value, 'Unsaved location')
 assert.equal(e.fishName.value, 'Unsaved fish')
 p.apply({sessions:[], fishNames:[], rates:emptyRates, ratesByLocation:{}})
 assert.deepEqual(e.catchLocations.children, [])
 assert.deepEqual(e.fishNames.children, [])
})

test('location and fish inputs use saved suggestions with browser autocomplete off', () => {
 const p = page()
 for (const [id, list] of [['catchLocation', 'catchLocations'], ['fishName', 'fishNames']]) {
  const input = p.html.match(new RegExp(`<input id="${id}"[^>]*>`))[0]
  assert.ok(input.includes(`list="${list}"`))
  assert.ok(input.includes('autocomplete="off"'))
 }
 const row = p.edit({fish:'Catfish', amount:2})
 const fields = row.children[0].children[0].children[0]
 const fish = fields.children[2].children[0].children[1]
 assert.equal(fish.name, 'fish')
 assert.equal(fish.list, 'fishNames')
 assert.equal(fish.autocomplete, 'off')
})

test('location selection filters every rate display and survives refreshed logs', () => {
 const p = page(), e = p.elements
 const data = {sessions:[session('Stormwind', 3600), session('Goldshire', 3600),
  session('Goldshire', 0, [{fish:'Goldfish', amount:1}])], fishNames:[], rates:emptyRates,
  ratesByLocation:{Stormwind:rates('Catfish', 20), Goldshire:rates('Goldfish', 10)}}
 p.apply(data)
 assert.deepEqual(e.rateLocation.children.map(option => option.textContent), ['Goldshire', 'Stormwind'])
 assert.equal(e.rateLocation.value, 'Goldshire')
 assert.equal(e.rateLocation.disabled, false)
 assert.equal(e.totalCatchRate.textContent, '10.0')
 assert.equal(e.rateSessions.textContent, '2 sessions')
 assert.match(e.rateWarning.textContent, /0 seconds/)

 e.rateLocation.value = 'Stormwind'
 e.rateLocation.onchange()
 assert.equal(e.totalCatchRate.textContent, '20.0')
 assert.equal(e.totalStackRate.textContent, '1.0')
 assert.equal(e.rateSessions.textContent, '1 session')
 assert.equal(e.rateWarning.textContent, '')
 assert.match(e.rateContext.textContent, /^20 fish caught/)
 assert.equal(e.catchRateRows.children.length, 1)
 assert.equal(e.catchRateRows.children[0].children[0].textContent, 'Catfish')
 assert.match(e.stackBreakdownRows.children[0].textContent, /^Catfish: 20 fish/)
 assert.match(e.stackBreakdownTotal.textContent, /1 stack \/ 1 hours = 1.0 stacks\/hour/)

 const refreshed = structuredClone(data)
 refreshed.ratesByLocation.Stormwind = rates('Catfish', 15)
 refreshed.sessions.push(session('Booty Bay', 3600))
 refreshed.ratesByLocation['Booty Bay'] = rates('Tuna', 5)
 p.apply(refreshed)
 assert.equal(e.rateLocation.value, 'Stormwind')
 assert.equal(e.totalCatchRate.textContent, '15.0')

 const deleted = structuredClone(refreshed)
 deleted.sessions = deleted.sessions.filter(log => log.location !== 'Stormwind')
 delete deleted.ratesByLocation.Stormwind
 p.apply(deleted)
 assert.equal(e.rateLocation.value, 'Booty Bay')
 assert.equal(e.totalCatchRate.textContent, '5.0')
})

test('empty logs clear location options, rates and breakdowns', () => {
 const p = page(), e = p.elements
 p.apply({sessions:[session('Stormwind', 3600)], fishNames:[], rates:rates('Catfish', 20),
  ratesByLocation:{Stormwind:rates('Catfish', 20)}})
 p.apply({sessions:[], fishNames:[], rates:emptyRates, ratesByLocation:{}})
 assert.equal(e.rateLocation.disabled, true)
 assert.equal(e.rateLocation.value, '')
 assert.equal(e.rateLocation.children[0].textContent, 'No locations yet')
 assert.equal(e.totalCatchRate.textContent, 'Not available')
 assert.equal(e.totalStackRate.textContent, 'Not available')
 assert.equal(e.rateSessions.textContent, '0 sessions')
 assert.equal(e.catchRateRows.children.length, 0)
 assert.equal(e.stackBreakdownRows.children.length, 0)
 assert.match(e.rateWarning.textContent, /Create a session log/)
})
