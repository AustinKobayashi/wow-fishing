'use strict'
const $ = id => document.getElementById(id)
let schema, defaults, cfg, saved, token, currentState
let mode = 'reel', dirty = false, connected = false, busy = false, previewValid = false
let previewVersion = 0, previewTimer, hiddenThrough = 0, entries = []
let catchData, catchBusy = false
const helps = {
 timing:'Minimum is enforced. Spread reference is not a ceiling. Cast mean follows each reel sample.',
 detection:'The loop also sleeps 0.01 s and reads 1024 frames. Check interval alone does not set detection latency. The audio device must support your sample rate and channel count.',
 session:'Idle duration is uniform between the bounds. Probability is evaluated once per detected event, after the reel decision.',
 servo:'Each button action holds press and release for this duration. GPIO 27 stays fixed. Zero pulse width disables pulses, rather than commanding a calibrated return position.'
}
async function api(path, payload) {
 const controller = new AbortController()
 const timeout = setTimeout(() => controller.abort(), 10000)
 try {
  const response = await fetch(path, {
   signal:controller.signal,
   headers:payload === undefined ? {} : {'Content-Type':'application/json','X-Control-Token':token},
   method:payload === undefined ? 'GET' : 'POST',
   body:payload === undefined ? undefined : JSON.stringify(payload)
  })
  const data = await response.json()
  if (!response.ok) throw new Error(data.error || 'Request failed')
  return data
 } finally { clearTimeout(timeout) }
}
function controls() {
 const active = !!currentState?.active
 $('start').disabled = !connected || busy || active || dirty || !previewValid
 $('stop').disabled = !connected || busy || !active || currentState.status === 'Stopping'
 $('save').disabled = !connected || busy || active || !dirty || !previewValid
 $('reset').disabled = !connected || busy || active
 document.querySelectorAll('#fields input').forEach(el => el.disabled = !connected || busy || active)
 $('configState').textContent = dirty ? 'Unsaved changes' : 'Saved settings'
 const catchDisabled = !connected || catchBusy || !catchData?.sessions.length
 for (const id of ['catchSession', 'fishName', 'catchAmount', 'catchStacks', 'addCatch', 'exportCatches']) $(id).disabled = catchDisabled
 for (const id of ['catchLocation', 'sessionLength', 'createCatchLog']) $(id).disabled = !connected || catchBusy
}
function drawCatches() {
 const session = catchData?.sessions[Number($('catchSession').value)]
 $('catchRows').replaceChildren()
 let total = 0
 for (const catchEntry of session?.catches || []) {
  const row = document.createElement('tr'), fish = document.createElement('td'), amount = document.createElement('td')
  fish.textContent = catchEntry.fish
  amount.textContent = catchEntry.amount.toLocaleString()
  const stacks = document.createElement('td')
  stacks.textContent = catchEntry.stacks === false ? 'No' : 'Yes'
  total += catchEntry.amount
  row.append(fish, amount, stacks)
  $('catchRows').append(row)
 }
 $('catchTotal').textContent = total.toLocaleString() + ' fish logged'
 $('catchDuration').textContent = session ? `${session.location} | ${session.sessionLength.toLocaleString()} seconds | Created ${new Date(session.date).toLocaleString()}` : 'Create a session log above to begin.'
 controls()
}
function applyCatches(data) {
 if (JSON.stringify(catchData) === JSON.stringify(data)) return
 const previous = $('catchSession').value
 const newSession = !catchData || data.sessions.length !== catchData.sessions.length
 catchData = data
 $('catchSession').replaceChildren()
 data.sessions.forEach((session, index) => {
  const option = document.createElement('option')
  option.value = index
  option.textContent = `${index + 1}. ${session.location} | ${new Date(session.date).toLocaleString()} | ${session.sessionLength.toLocaleString()} s`
  $('catchSession').prepend(option)
 })
 $('catchSession').value = newSession ? String(data.sessions.length - 1) : previous
 $('fishNames').replaceChildren()
 data.fishNames.forEach(name => {
  const option = document.createElement('option')
  option.value = name
  $('fishNames').append(option)
 })
 drawCatches()
 drawCatchRates()
}
function drawCatchRates() {
 const {rates, sessions} = catchData
 const rateText = value => value === null ? 'Not available' : value.toLocaleString(undefined, {minimumFractionDigits:1, maximumFractionDigits:1})
 $('totalCatchRate').textContent = rateText(rates.perHour)
 $('totalStackRate').textContent = rateText(rates.stacksPerHour)
 $('stackBreakdownRows').replaceChildren()
 for (const entry of rates.catches) {
  const item = document.createElement('li')
  const stackingAmount = entry.amount - entry.nonStackingAmount
  const full = Math.floor(stackingAmount / 20), remainder = stackingAmount % 20
  const parts = [full ? `${full.toLocaleString()} full` : '', remainder ? `1 partial (${remainder}/20)` : '', entry.nonStackingAmount ? `${entry.nonStackingAmount.toLocaleString()} non-stacking (1 slot each)` : ''].filter(Boolean).join(' + ')
  item.textContent = `${entry.fish}: ${entry.amount.toLocaleString()} fish = ${entry.stacks.toLocaleString()} ${entry.stacks === 1 ? 'stack' : 'stacks'}: ${parts}`
  $('stackBreakdownRows').append(item)
 }
 $('stackBreakdownTotal').textContent = `${rates.stacks.toLocaleString()} ${rates.stacks === 1 ? 'stack' : 'stacks'} / ${(rates.seconds / 3600).toLocaleString(undefined, {maximumFractionDigits:2})} hours = ${rateText(rates.stacksPerHour)} stacks/hour`
 $('rateSessions').textContent = `${sessions.length} ${sessions.length === 1 ? 'session' : 'sessions'}`
 $('rateContext').textContent = `${rates.amount.toLocaleString()} fish caught · ${(rates.seconds / 3600).toLocaleString(undefined, {maximumFractionDigits:2})} hours fished`
 $('catchRateRows').replaceChildren()
 for (const entry of rates.catches) {
  const row = document.createElement('tr')
  for (const value of [entry.fish, entry.amount.toLocaleString(), rateText(entry.perHour)]) {
   const cell = document.createElement('td')
   cell.textContent = value
   row.append(cell)
  }
  $('catchRateRows').append(row)
 }
 $('rateWarning').textContent = !sessions.length ? 'Create a session log to see your catch rates.' : !rates.seconds ? 'An hourly rate needs recorded fishing time.' : sessions.some(session => !session.sessionLength && session.catches.length) ? 'Some catches have a session duration of 0 seconds. Rates may be overstated.' : ''
}
async function refreshCatches() {
 if (catchBusy) return
 try {
  const data = await api('/api/catches')
  if (!catchBusy) {
   applyCatches(data)
   $('catchLoadError').textContent = ''
  }
 }
 catch (error) { $('catchLoadError').textContent = error.message }
}
function fill(values) {
 cfg = {...values}
 Object.entries(values).forEach(([key,value]) => $(key).value = value)
}
function readFields() {
 const values = {}
 Object.values(schema).flat().forEach(([key]) => {
  const el = $(key)
  if (el.value === '' || !el.checkValidity()) throw Error('Check ' + key + ' and its allowed range.')
  values[key] = Number(el.value)
 })
 return values
}
function previewError(message) {
 previewValid = false
 $('error').textContent = message
 $('chart').replaceChildren()
 $('metrics').textContent = 'Preview unavailable for these inputs.'
 $('scenarioResult').textContent = 'Correct the inputs to calculate probabilities.'
 controls()
}
async function updatePreview() {
 const version = ++previewVersion
 previewValid = false
 controls()
 try {
  const values = readFields(), input = $('scenarioInput')
  if (!input.checkValidity() || input.value === '') throw Error('Enter a valid scenario time.')
  const result = await api('/api/preview', {config:values, mode, scenario:Number(input.value)})
  if (version !== previewVersion) return
  cfg = values
  render(result)
  previewValid = true
  $('error').textContent = ''
  controls()
 } catch (error) {
  if (version === previewVersion) previewError(error.message)
 }
}
const pct = x => x > 0 && x < .0001 ? '<0.01%' : x < 1 && x > .9999 ? '>99.99%' : (x * 100).toFixed(2) + '%'
function render(m) {
 const reel = m.mode === 'reel', cut = m.cutoff
 const X = x => 52 + (x - m.xmin) / (m.xmax - m.xmin) * 600
 const Y = y => 207 - y / m.ymax * 170
 const path = points => points.map(([x,y]) => `${X(x).toFixed(2)},${Y(y).toFixed(2)}`).join(' ')
 let svg = ''
 for (let i = 0; i < 5; i++) {
  const y = m.ymax * i / 4
  svg += `<line x1="52" y1="${Y(y)}" x2="652" y2="${Y(y)}" stroke="#424735"/><text x="43" y="${Y(y)+4}" text-anchor="end" fill="#aaa991" font-size="10">${y.toFixed(1)}</text>`
 }
 for (let i = 0; i <= 6; i++) {
  const x = m.xmin + (m.xmax - m.xmin) * i / 6
  svg += `<text x="${X(x)}" y="227" text-anchor="middle" fill="#aaa991" font-size="10">${x.toFixed(2)}</text>`
 }
 svg += '<text x="52" y="16" fill="#aaa991" font-size="10">Density (1/s)</text><text x="652" y="246" text-anchor="end" fill="#aaa991" font-size="10">Delay (seconds) →</text>'
 svg += `<polygon points="${X(m.minimum)},207 ${path(m.points)} ${X(m.xmax)},207" fill="#85bcae" fill-opacity=".12"/><polyline points="${path(m.points)}" fill="none" stroke="#85bcae" stroke-width="2.5"/>`
 if (reel && cut < m.xmax) {
  const tail = m.points.filter(p => p[0] >= Math.max(cut,m.minimum))
  svg += `<polygon points="${X(tail[0][0])},207 ${path(tail)} ${X(m.xmax)},207" fill="#e7bd6e" fill-opacity=".3"/>`
 }
 if (reel && cut >= m.xmin && cut <= m.xmax) {
  svg += `<line x1="${X(cut)}" y1="30" x2="${X(cut)}" y2="207" stroke="#e7bd6e" stroke-dasharray="4 4"/><text x="${Math.min(555,Math.max(62,X(cut)+8))}" y="28" fill="#e7bd6e" font-size="11">Cutoff ${cut.toFixed(2)} s</text>`
 }
 $('chart').innerHTML = svg
 $('chart').setAttribute('aria-label', reel ? 'Reel delay density. Cutoff skips ' + pct(m.cutoff_skip) : 'Cast delay density conditional on reel sample ' + m.scenario + ' seconds')
 $('chartTitle').textContent = reel ? 'Most reactions. A few deliberate skips.' : 'The next cast follows the reel sample.'
 $('chartSub').textContent = `Lower-truncated normal · underlying μ ${m.mu.toFixed(3)} s · σ ${m.sigma.toFixed(3)} s${dirty ? ' · unsaved draft' : ''}`
 $('cutoffLegend').classList.toggle('hidden', !reel)
 const metrics = reel ? [['Mean accepted delay',m.mean.toFixed(3)+' s'],['Cutoff skips',pct(m.cutoff_skip)],['Per 1,000 detections','≈ '+(1000*m.cutoff_skip).toFixed(1)]] : [['Mean accepted delay',m.mean.toFixed(3)+' s'],['Conditional reel sample',m.scenario.toFixed(3)+' s'],['Above spread reference',pct(m.above_reference)]]
 $('metrics').replaceChildren()
 metrics.forEach(([label,value]) => {
  const wrapper = document.createElement('div'), title = document.createElement('div'), number = document.createElement('strong')
  wrapper.className = 'metric'
  title.className = 'small'
  title.textContent = label
  number.textContent = value
  wrapper.append(title,number)
  $('metrics').append(wrapper)
 })
 $('scenarioResult').textContent = !reel ? 'This curve uses the supplied reel sample as its mean before minimum truncation. Cast delay has no DONT_REEL_TIME cutoff. This is a conditional curve, not the overall cast distribution.' : m.guard_blocks ? 'At this age, the post-cast guard blocks detection. The timing branch would not run.' : m.recast_due ? 'At this age, the recast timeout is already due. The loop would recast before checking audio. Choose an earlier detection age.' : `At detection age ${m.scenario.toFixed(1)} s, the combined skip estimate is ${pct(m.combined_skip)} (≈ ${(1000*m.combined_skip).toFixed(1)} per 1,000 detections). Effective delay limit: ${m.effective_limit.toFixed(3)} s. Assumes negligible processing overhead. This is not a catch-success forecast.`
}
function drawLogs() {
 const filter = $('filter').value.toLowerCase()
 const atBottom = $('logs').scrollHeight - $('logs').scrollTop - $('logs').clientHeight < 35
 $('logs').replaceChildren()
 entries.filter(e => e.id > hiddenThrough && e.message.toLowerCase().includes(filter)).forEach(e => {
  const row = document.createElement('div'), time = document.createElement('time'), tag = document.createElement('b'), msg = document.createElement('span')
  row.className = 'log-line'
  time.textContent = e.time
  tag.textContent = 'BOT'
  msg.textContent = e.message
  row.append(time,tag,msg)
  $('logs').append(row)
 })
 if (atBottom) $('logs').scrollTop = $('logs').scrollHeight
}
function applyState(state) {
 currentState = state
 connected = true
 $('connection').textContent = 'Controller connected'
 $('notice').classList.remove('connection-error')
 $('notice').textContent = state.error ? 'Session error: ' + state.error : state.active ? 'LIVE SESSION · The bot runs on the server. Closing this page does not stop it. Use Stop fishing to end the session.' : 'LOCAL CONTROL · Save your settings, then cast a line. Starting will operate the connected servo and audio input.'
 $('status').textContent = state.status
 const seconds = Math.floor(state.elapsed)
 $('elapsed').textContent = [Math.floor(seconds/3600),Math.floor(seconds/60)%60,seconds%60].map(x => String(x).padStart(2,'0')).join(':')
 $('threshold').textContent = state.config.THRESHOLD.toLocaleString()
 $('cutoffCard').textContent = state.config.DONT_REEL_TIME.toFixed(3) + ' s'
 $('runlimit').textContent = (state.config.MAX_RUN_TIME/3600).toFixed(2) + ' hour run limit'
 // Polling never overwrites an unsaved local draft.
 if (saved && JSON.stringify(saved) !== JSON.stringify(state.config) && !dirty) {
  fill(state.config)
  updatePreview()
 }
 saved = {...state.config}
 const changed = JSON.stringify(entries) !== JSON.stringify(state.logs)
 entries = state.logs
 if (changed) drawLogs()
 controls()
}
async function action(path,payload) {
 busy = true
 controls()
 try {
  const result = await api(path,payload)
  if (path === '/api/config') {
   saved = {...result.config}
   dirty = false
   fill(saved)
   await updatePreview()
   applyState(await api('/api/state'))
  } else applyState(result)
 } catch (error) {
  $('notice').textContent = error.message
  $('notice').classList.add('connection-error')
 } finally {
  busy = false
  controls()
 }
}
async function poll() {
 try {
  applyState(await api('/api/state'))
 }
 catch (error) {
  connected = false
  $('connection').textContent = 'Controller disconnected'
  $('status').textContent = 'Unknown'
  $('notice').textContent = 'Connection lost. The bot may still be running on the server. Reconnecting...'
  $('notice').classList.add('connection-error')
  controls()
 } finally { setTimeout(poll,1000) }
}
async function init() {
 try {
  const boot = await api('/api/bootstrap')
  schema = boot.schema
  defaults = boot.defaults
  token = boot.token
  saved = boot.state.config
  Object.entries(schema).forEach(([tab,items]) => {
   const group = document.createElement('div')
   group.className = 'group' + (tab === 'timing' ? '' : ' hidden')
   group.id = 'group-' + tab
   items.forEach(([key,label,unit,value,min,max,step]) => {
    const field = document.createElement('div')
    field.className = 'field'
    field.innerHTML = `<label for="${key}">${label} <span class="small">(${unit})</span></label><input id="${key}" type="number" min="${min}" max="${max}" step="${step}"><code>${key}</code>`
    group.append(field)
   })
   $('fields').append(group)
  })
  fill(saved)
  $('configHelp').textContent = helps.timing
  applyState(boot.state)
  if ($('catchLog').open) await refreshCatches()
  await updatePreview()
  poll()
 } catch (error) {
  $('notice').textContent = 'Could not load the controller: ' + error.message + '. Refresh to retry.'
  $('notice').classList.add('connection-error')
 }
}
$('configTabs').onclick = e => {
 const tab = e.target.dataset.tab
 if (!tab || !schema) return
 document.querySelectorAll('#configTabs button').forEach(b => b.classList.toggle('active', b.dataset.tab === tab))
 Object.keys(schema).forEach(k => $('group-'+k).classList.toggle('hidden',k !== tab))
 $('configHelp').textContent = helps[tab]
}
$('fields').oninput = () => {
 dirty = true
 previewValid = false
 ++previewVersion
 controls()
 clearTimeout(previewTimer)
 previewTimer = setTimeout(updatePreview,200)
}
$('scenarioInput').oninput = updatePreview
$('graphTabs').onclick = e => {
 if (!e.target.dataset.mode || !cfg) return
 mode = e.target.dataset.mode
 document.querySelectorAll('#graphTabs button').forEach(b => b.classList.toggle('active',b.dataset.mode === mode))
 $('scenarioLabel').textContent = mode === 'reel' ? 'Detection age after last cast' : 'Previous reel sample'
 $('scenarioHelp').textContent = mode === 'reel' ? 'Scenario only. Estimates the fishing-window effect.' : 'Scenario only. This value becomes the cast mean.'
 $('scenarioInput').value = mode === 'reel' ? 10 : Math.max(cfg.REEL_TIME_MIN,1.4)
 updatePreview()
}
$('reset').onclick = () => { fill(defaults)
 dirty = true
 updatePreview() }
$('save').onclick = () => { try { action('/api/config',{config:readFields()}) } catch (error) { previewError(error.message) } }
$('start').onclick = () => action('/api/start',{})
$('stop').onclick = () => action('/api/stop',{})
$('catchNav').onclick = () => { $('catchLog').open = true }
$('catchLog').ontoggle = () => { if ($('catchLog').open && connected) refreshCatches() }
$('catchSession').onchange = drawCatches
$('stackRateStat').onmouseenter = () => { $('stackBreakdown').hidden = false }
$('stackRateStat').onmouseleave = () => { if (document.activeElement !== $('stackRate')) $('stackBreakdown').hidden = true }
$('stackRate').onfocus = $('stackRate').onclick = () => { $('stackBreakdown').hidden = false }
$('stackRate').onblur = () => { $('stackBreakdown').hidden = true }
$('stackRate').onkeydown = e => { if (e.key === 'Escape') $('stackBreakdown').hidden = true }
function selectCatchTab(tab) {
 document.querySelectorAll('#catchTabs button').forEach(button => {
  const selected = button === tab
  button.classList.toggle('active', selected)
  button.setAttribute('aria-selected', String(selected))
  button.tabIndex = selected ? 0 : -1
  $(button.getAttribute('aria-controls')).hidden = !selected
 })
 if (connected) refreshCatches()
}
$('catchTabs').onclick = e => {
 if (e.target.matches('button')) selectCatchTab(e.target)
}
$('catchTabs').onkeydown = e => {
 if (!['ArrowLeft', 'ArrowRight', 'Home', 'End'].includes(e.key)) return
 e.preventDefault()
 const tab = e.key === 'Home' ? $('sessionLogsTab') : e.key === 'End' ? $('catchRatesTab') : e.target === $('sessionLogsTab') ? $('catchRatesTab') : $('sessionLogsTab')
 selectCatchTab(tab)
 tab.focus()
}
$('newCatchLog').onsubmit = async e => {
 e.preventDefault()
 if (catchBusy) return
 catchBusy = true
 controls()
 $('catchError').textContent = ''
 $('catchMessage').textContent = ''
 try {
  applyCatches(await api('/api/catches/new', {sessionLength:Number($('sessionLength').value), location:$('catchLocation').value}))
  $('catchMessage').textContent = 'Session log saved. Add your catches below.'
 } catch (error) { $('catchError').textContent = error.message }
 finally {
  catchBusy = false
  controls()
 }
}
$('catchForm').onsubmit = async e => {
 e.preventDefault()
 if (catchBusy) return
 const session = Number($('catchSession').value), fish = $('fishName').value, amount = Number($('catchAmount').value)
 catchBusy = true
 controls()
 $('catchError').textContent = ''
 $('catchMessage').textContent = ''
 try {
  applyCatches(await api('/api/catches', {session, fish, amount, stacks:$('catchStacks').checked}))
  $('catchMessage').textContent = `Added ${amount} ${fish.trim()} to session ${session + 1}.`
 } catch (error) { $('catchError').textContent = error.message }
 finally {
  catchBusy = false
  controls()
 }
}
$('exportCatches').onclick = async () => {
 const sessionIndex = Number($('catchSession').value)
 try {
  const data = await api('/api/catches')
  const url = URL.createObjectURL(new Blob([JSON.stringify(data.sessions[sessionIndex], null, 2) + '\n'], {type:'application/json'}))
  const link = document.createElement('a')
  link.href = url
  link.download = `fishing-session-${sessionIndex + 1}.json`
  link.click()
  setTimeout(() => URL.revokeObjectURL(url), 1000)
 } catch (error) { $('catchError').textContent = error.message }
}
$('filter').oninput = drawLogs
$('clear').onclick = () => { hiddenThrough = entries.at(-1)?.id || 0
 drawLogs() }
$('download').onclick = () => {
 const url = URL.createObjectURL(new Blob([entries.map(e => e.time + ' ' + e.message).join('\n')],{type:'text/plain'}))
 const link = document.createElement('a')
 link.href = url
 link.download = 'anglers-rest-recent.log'
 link.click()
 setTimeout(() => URL.revokeObjectURL(url),1000)
}
window.addEventListener('beforeunload',e => { if (dirty) { e.preventDefault()
 e.returnValue = '' } })
init()
