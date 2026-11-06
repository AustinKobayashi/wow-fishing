"""Fishing runtime shared by the command line and local web controller."""
import signal
import threading
import time
from collections import deque

import numpy as np
import logger as lg
import normal_distribution as nd
from configuration import STATS_PATH, load_config, validate
from servo import Servo


class Stopped(Exception):
    pass


def main(config=None, stop_event=None, log=lg.log, on_ready=lambda: None,
         audio_module=None, servo_factory=Servo):
    config = validate(config) if config is not None else load_config()
    stop_event = stop_event if stop_event is not None else threading.Event()
    start = time.monotonic()
    deadline = start + config['MAX_RUN_TIME']
    audio = stream = servo = None
    reel_times, cast_times = deque(maxlen=10000), deque(maxlen=10000)
    volume_sum = max_volume = detections = idle_counts = 0

    def wait(duration):
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise Stopped('Maximum runtime reached.')
        if stop_event.wait(min(max(duration, 0), remaining)):
            raise Stopped('Stop requested.')
        if time.monotonic() >= deadline:
            raise Stopped('Maximum runtime reached.')

    try:
        if audio_module is None:
            import pyaudio as audio_module
        wait(0)
        audio = audio_module.PyAudio()
        stream = audio.open(format=audio_module.paInt16,
                            channels=config['CHANNELS'], rate=config['RATE'],
                            input=True, frames_per_buffer=1024)
        while not stream.is_active():
            log('Waiting for audio stream to be active')
            wait(.1)
        wait(0)
        servo = servo_factory(config, wait, log)
        # Preserve the existing cast timestamp and independent reel cutoff semantics.
        last_cast = time.monotonic()
        servo.press_fishing_button('Casting')
        on_ready()
        log('Listening for a bite.')
        while True:
            wait(0)
            servo.set_neutral()
            if time.monotonic() - last_cast > config['LAST_CAST_MAX']:
                log('Did not cast, recasting...')
                servo.press_fishing_button('Casting')
                last_cast = time.monotonic()
            wait(.01)
            # Read only available frames so stop requests are checked while waiting.
            queued = stream.get_read_available()
            while queued > 0:
                wait(0)
                size = min(queued, 1024)
                stream.read(size, exception_on_overflow=False)
                queued -= size
            while stream.get_read_available() < 1024:
                if not stream.is_active():
                    raise RuntimeError('Audio input stream stopped.')
                wait(.01)
            wait(0)
            data = np.frombuffer(stream.read(1024, exception_on_overflow=False), dtype=np.int16)
            # int32 avoids abs(-32768) overflowing an int16 sample.
            level = float(np.abs(data.astype(np.int32)).mean())
            if level > config['THRESHOLD'] and time.monotonic() - last_cast > config['REEL_TIME_DELAY_AFTER_CAST']:
                detections += 1
                volume_sum += level
                max_volume = max(max_volume, level)
                log(f'Audio level above threshold: {level:.1f}')
                reel = float(nd.for_config(config, 'REEL_TIME').rvs())
                reel_times.append(reel)
                log(f'Reel time: {reel:.6f}')
                wait(reel)
                # Intentionally counts reel delay again, matching the existing bot.
                if time.monotonic() - last_cast + reel < config['WOW_FISHING_TIME'] and reel < config['DONT_REEL_TIME']:
                    servo.press_fishing_button('Reeling')
                else:
                    log('Skipping reel: hard delay cutoff or fishing window reached.')
                    wait(max(config['WOW_FISHING_TIME'] - (time.monotonic() - last_cast), .3))
                if np.random.rand() < config['IDLE_PROBABILITY']:
                    idle_counts += 1
                    duration = np.random.uniform(config['IDLE_TIME_MIN'], config['IDLE_TIME_MAX'])
                    log(f'Idle for {duration:.2f} seconds')
                    wait(duration)
                    last_cast = time.monotonic()
                cast = float(nd.for_config(config, 'CAST_TIME', reel).rvs())
                cast_times.append(cast)
                log(f'Cast time: {cast:.6f}')
                wait(cast)
                servo.press_fishing_button('Casting')
                last_cast = time.monotonic()
            wait(config['CHECK_INTERVAL'])
    except Stopped as exc:
        log(str(exc))
    finally:
        # Try every cleanup even if one device fails. Report failures to the UI.
        cleanup_errors = []
        for resource, method in ((servo, 'cleanup'), (stream, 'close'), (audio, 'terminate')):
            if resource is not None:
                try:
                    getattr(resource, method)()
                except Exception as exc:
                    cleanup_errors.append(f'{method}: {exc}')
                    log(f'Cleanup error ({method}): {exc}')
        if stream is not None:
            summary = (f'Maximum volume: {max_volume}\n'
                       f'Mean volume: {volume_sum / detections if detections else 0}\n'
                       f'Detected events: {detections}\nIdle counts: {idle_counts}\n'
                       f'Timing history: latest 10000 samples per kind\n'
                       f'Reel times: {list(reel_times)}\nCast times: {list(cast_times)}\n')
            STATS_PATH.parent.mkdir(parents=True, exist_ok=True)
            STATS_PATH.write_text(summary, encoding='utf-8')
            log(f'Session ended. Detected events: {detections}, idle periods: {idle_counts}.')
        if cleanup_errors:
            raise RuntimeError('Hardware cleanup failed: ' + ', '.join(cleanup_errors))


if __name__ == '__main__':
    stop = threading.Event()
    signal.signal(signal.SIGINT, lambda *_: stop.set())
    signal.signal(signal.SIGTERM, lambda *_: stop.set())
    try:
        main(stop_event=stop)
    except Exception as exc:
        lg.log(f'Bot error: {exc}')
        raise
