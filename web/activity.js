'use strict'
{
 const box = document.getElementById('activityBox')
 const name = document.getElementById('activityName')
 const timer = document.getElementById('activityTimer')
 const caption = document.getElementById('activityCaption')
 const detail = document.getElementById('activityDetail')
 const next = document.getElementById('activityNext')
 const fill = document.getElementById('activityFill')
 const audioLevel = document.getElementById('audioLevel')
 const audioLive = document.getElementById('audioLive')
 const audioStatus = document.getElementById('audioStatus')
 const audioScale = document.getElementById('audioScale')
 const audioThresholdValue = document.getElementById('audioThresholdValue')
 const audioThreshold = document.getElementById('audioThreshold')
 const audioLine = document.getElementById('audioLine')
 const audioArea = document.getElementById('audioArea')
 const audioDot = document.getElementById('audioDot')
 const stages = {
  starting:['Starting', 'Connecting audio and servo.', 'Casting'],
  paused:['Paused', 'Five consecutive casts failed. The session timer is paused.', 'Cast a line to resume, or Stop fishing to end the session'],
  casting:['Casting', 'Pressing the fishing key, then releasing it.', 'Waiting for bite audio'],
  listening:['Waiting for bite audio', 'Listening for audio above the bite threshold.', 'Reaction delay on detection, or recast on timeout'],
  reaction:['Waiting reaction delay', 'Bite audio detected. Waiting before the reel check.', 'Press reel key, or skip if the cutoff is reached'],
  reeling:['Pressing reel key', 'Pressing the fishing key, then releasing it.', 'Cast delay, with a possible idle break first'],
  skipping:['Waiting out fishing window', 'Reel skipped because the delay or window cutoff was reached.', 'Cast delay, with a possible idle break first'],
  idle:['Taking idle break', 'A random pause before the next cast.', 'Waiting before next cast'],
  castdelay:['Waiting before next cast', 'Cast delay sampled from the previous reel delay.', 'Casting'],
  stopping:['Stopping', 'Waiting for the bot to finish hardware cleanup.', 'Stopped'],
  stopped:['Stopped', 'No active fishing cycle.', 'Cast a line to start fishing'],
  error:['Session error', 'See the fishing journal for details.', 'Resolve the error before starting again'],
  unavailable:['State unavailable', 'Connection lost. The bot may still be running.', 'Waiting to reconnect']
 }
 let stage = null, receivedAt = 0, source
 let audio = null, threshold = null, lastSampled = null, history = []
 function drawAudio() {
  const now = performance.now()
  const age = audio ? audio.age * 1000 + now - receivedAt : Infinity
  if (audio && audio.sampled !== lastSampled) {
   lastSampled = audio.sampled
   if (age <= 1000) history.push({time: now - age, level: audio.level})
  }
  history = history.filter(point => now - point.time <= 1000).slice(-11)
  const live = stage && stage.name === 'listening' && age <= 500
  audioLevel.textContent = live ? Math.round(audio.level).toLocaleString('en-US') : '--'
  audioLive.textContent = !stage ? 'OFFLINE' : live ? 'LIVE' : 'INACTIVE'
  const status = !stage ? 'Connection unavailable' : live ? (audio.level > threshold ? 'Above threshold' : 'Below threshold') :
   stage.name === 'listening' ? 'Waiting for an audio reading' : 'Not sampling during this activity'
  if (audioStatus.textContent !== status) audioStatus.textContent = status
  const ceiling = Math.min(32768, Math.max(12000, threshold * 1.5, ...history.map(point => point.level)))
  const y = level => 98 - level / ceiling * 96
  audioScale.textContent = Math.round(ceiling).toLocaleString('en-US')
  audioThresholdValue.textContent = threshold === null ? '--' : threshold.toLocaleString('en-US')
  audioThreshold.setAttribute('visibility', threshold === null ? 'hidden' : 'visible')
  audioThreshold.setAttribute('y1', y(threshold))
  audioThreshold.setAttribute('y2', y(threshold))
  let line = '', area = '', start = null, previous = null
  for (const point of history) {
   const x = 2 + (1 - (now - point.time) / 1000) * 296
   const top = y(point.level)
   const gap = !previous || point.time - previous.time > 250
   if (gap) {
    if (previous) area += ` L${previous.x},98 L${start},98 Z`
    start = x
   }
   const segment = `${gap ? 'M' : 'L'}${x},${top}`
   line += segment
   area += segment
   previous = {...point, x, top}
  }
  if (previous) {
   area += ` L${previous.x},98 L${start},98 Z`
   audioDot.setAttribute('cx', previous.x)
   audioDot.setAttribute('cy', previous.top)
  }
  audioLine.setAttribute('d', line)
  audioArea.setAttribute('d', area)
  audioDot.setAttribute('visibility', previous ? 'visible' : 'hidden')
 }
 function describe(key) {
  const labels = stages[key]
  name.textContent = labels[0]
  detail.textContent = labels[1]
  next.textContent = 'Next: ' + labels[2]
  box.dataset.stage = key
 }
 function unavailable() {
  stage = null
  audio = null
  history = []
  lastSampled = null
  drawAudio()
  describe('unavailable')
  timer.textContent = 'Unknown'
  caption.textContent = 'No live timing available'
  fill.style.width = '0%'
 }
 function drawTimer() {
  if (!stage) return
  const sinceUpdate = (performance.now() - receivedAt) / 1000
  if (sinceUpdate > 6) {
   unavailable()
   return
  }
  if (stage.duration === null) {
   timer.textContent = stage.name === 'paused' ? 'Timer paused' : stage.name === 'stopped' ? 'Ready' : stage.name === 'error' ? 'Attention needed' : 'Please wait'
   caption.textContent = 'No active countdown'
   fill.style.width = '0%'
   return
  }
  const elapsed = stage.elapsed + sinceUpdate
  const remaining = Math.max(0, stage.duration - elapsed)
  fill.style.width = (stage.duration > 0 ? Math.min(100, 100 * remaining / stage.duration) : 0) + '%'
  if (stage.name === 'listening') {
   timer.textContent = elapsed.toFixed(1) + ' s listening'
   caption.textContent = remaining > 0 ? 'Recast in ' + remaining.toFixed(1) + ' s' : 'Recast due. Waiting for bot.'
  } else {
   timer.textContent = remaining.toFixed(1) + ' s remaining'
   caption.textContent = remaining > 0 ? 'Of ' + stage.duration.toFixed(1) + ' s in this stage' : 'Waiting for bot to change stage.'
  }
 }
 function connect() {
  source = new EventSource('/api/stages')
  source.addEventListener('stage', event => {
   try {
    const update = JSON.parse(event.data)
    if (!stages[update.name] || !Number.isFinite(update.id) || !Number.isFinite(update.elapsed) || update.elapsed < 0 ||
        !(update.duration === null || Number.isFinite(update.duration) && update.duration >= 0)) throw Error('Invalid stage')
    if (stage && update.id < stage.id) return
    if (!stage || update.id !== stage.id) describe(update.name)
    stage = update
    receivedAt = performance.now()
    threshold = Number.isFinite(update.threshold) && update.threshold >= 0 && update.threshold <= 32768 ? update.threshold : null
    const reading = update.audio
    audio = threshold !== null && reading && Number.isFinite(reading.level) && reading.level >= 0 && reading.level <= 32768 &&
     Number.isFinite(reading.sampled) && Number.isFinite(reading.age) && reading.age >= 0 ? reading : null
    drawTimer()
   } catch (_) { unavailable() }
  })
  source.onerror = unavailable
 }
 connect()
 function draw() { drawTimer()
  drawAudio() }
 let tick = setInterval(draw, 100)
 window.addEventListener('pagehide', () => { source.close()
  clearInterval(tick) })
 window.addEventListener('pageshow', event => { if (event.persisted) {
  unavailable()
  connect()
  tick = setInterval(draw, 100)
 } })
}
