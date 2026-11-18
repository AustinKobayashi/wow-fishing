'use strict'
{
 const box = document.getElementById('activityBox')
 const name = document.getElementById('activityName')
 const timer = document.getElementById('activityTimer')
 const caption = document.getElementById('activityCaption')
 const detail = document.getElementById('activityDetail')
 const next = document.getElementById('activityNext')
 const fill = document.getElementById('activityFill')
 const stages = {
  starting:['Starting', 'Connecting audio and servo.', 'Casting'],
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
 function describe(key) {
  const labels = stages[key]
  name.textContent = labels[0]
  detail.textContent = labels[1]
  next.textContent = 'Next: ' + labels[2]
  box.dataset.stage = key
 }
 function unavailable() {
  stage = null
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
   timer.textContent = stage.name === 'stopped' ? 'Ready' : stage.name === 'error' ? 'Attention needed' : 'Please wait'
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
    drawTimer()
   } catch (_) { unavailable() }
  })
  source.onerror = unavailable
 }
 connect()
 let tick = setInterval(drawTimer, 100)
 window.addEventListener('pagehide', () => { source.close()
  clearInterval(tick) })
 window.addEventListener('pageshow', event => { if (event.persisted) {
  unavailable()
  connect()
  tick = setInterval(drawTimer, 100)
 } })
}
