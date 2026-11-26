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


class PauseRequested(Exception):
    pass


def main(config=None, stop_event=None, log=lg.log, on_ready=lambda: None,
         audio_module=None, servo_factory=Servo, on_stage=lambda name, duration: None,
         on_pause=None, on_audio=lambda level: None, pause_event=None):
    config = validate(config) if config is not None else load_config()
    stop_event = stop_event if stop_event is not None else threading.Event()
    start = time.monotonic()
    deadline = start + config['MAX_RUN_TIME']
    audio = stream = servo = None
    reel_times, cast_times = deque(maxlen=10000), deque(maxlen=10000)
    volume_sum = max_volume = detections = idle_counts = 0
    consecutive_failed_casts = 0

    def wait(duration):
        until = min(time.monotonic() + max(duration, 0), deadline)
        while True:
            if stop_event.wait(0):
                raise Stopped('Stop requested.')
            if time.monotonic() >= deadline:
                raise Stopped('Maximum runtime reached.')
            if servo is not None and pause_event is not None and pause_event.is_set():
                raise PauseRequested()
            remaining = until - time.monotonic()
            if remaining <= 0:
                return
            if stop_event.wait(min(remaining, .1) if pause_event is not None else remaining):
                raise Stopped('Stop requested.')

    def listening():
        timeout = float(nd.for_config(config, 'RECAST_TIME').rvs())
        log(f'Recast timeout: {timeout:.2f} seconds')
        on_stage('listening', max(0, last_cast + config['WOW_FISHING_TIME'] - time.monotonic()))
        return last_cast + timeout

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
        while True:
            try:
                on_stage('startdelay', 3)
                wait(3)
                on_stage('casting', 2 * config['SERVO_HOLD_TIME'])
                servo.press_fishing_button('Casting')
                last_cast = time.monotonic()
                on_ready()
                recast_at = listening()
                log('Listening for a bite.')
                while True:
                    wait(0)
                    servo.set_neutral()
                    if time.monotonic() - last_cast >= config['WOW_FISHING_TIME']:
                        remaining = max(0, recast_at - time.monotonic())
                        on_stage('reconciling', remaining)
                        wait(remaining)
                        log('Did not cast, recasting...')
                        consecutive_failed_casts += 1
                        if consecutive_failed_casts >= 5:
                            if on_pause is None:
                                raise Stopped('Stopped after 5 consecutive failed casts.')
                            log('Pausing after 5 consecutive failed casts.')
                            raise PauseRequested()
                        on_stage('casting', 2 * config['SERVO_HOLD_TIME'])
                        servo.press_fishing_button('Casting')
                        last_cast = time.monotonic()
                        recast_at = listening()
                        continue
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
                        if time.monotonic() - last_cast >= config['WOW_FISHING_TIME']:
                            break
                        wait(.01)
                    wait(0)
                    if time.monotonic() - last_cast >= config['WOW_FISHING_TIME']:
                        continue
                    data = np.frombuffer(stream.read(1024, exception_on_overflow=False), dtype=np.int16)
                    # int32 avoids abs(-32768) overflowing an int16 sample.
                    level = float(np.abs(data.astype(np.int32)).mean())
                    on_audio(level)
                    age = time.monotonic() - last_cast
                    if (level > config['THRESHOLD']
                            and config['REEL_TIME_DELAY_AFTER_CAST'] < age < config['WOW_FISHING_TIME']):
                        consecutive_failed_casts = 0
                        detections += 1
                        volume_sum += level
                        max_volume = max(max_volume, level)
                        log(f'Audio level above threshold: {level:.1f}')
                        reel = float(nd.for_config(config, 'REEL_TIME').rvs())
                        reel_times.append(reel)
                        log(f'Reel time: {reel:.6f}')
                        on_stage('reaction', reel)
                        wait(reel)
                        remaining = max(0, last_cast + config['WOW_FISHING_TIME'] - time.monotonic())
                        if remaining > 0 and reel < config['DONT_REEL_TIME']:
                            on_stage('reeling', 2 * config['SERVO_HOLD_TIME'])
                            servo.press_fishing_button('Reeling')
                        else:
                            if remaining == 0:
                                log('Skipping reel: configured fishing channel duration elapsed.')
                            else:
                                log(f'Skipping reel: hard delay cutoff reached. Waiting {remaining:.2f} seconds for the fishing channel to end.')
                            on_stage('skipping', remaining)
                            wait(remaining)
                        if np.random.rand() < config['IDLE_PROBABILITY']:
                            idle_counts += 1
                            duration = np.random.uniform(config['IDLE_TIME_MIN'], config['IDLE_TIME_MAX'])
                            log(f'Idle for {duration:.2f} seconds')
                            on_stage('idle', float(duration))
                            wait(duration)
                            last_cast = time.monotonic()
                        cast = float(nd.for_config(config, 'CAST_TIME', reel).rvs())
                        cast_times.append(cast)
                        log(f'Cast time: {cast:.6f}')
                        on_stage('castdelay', cast)
                        wait(cast)
                        on_stage('casting', 2 * config['SERVO_HOLD_TIME'])
                        servo.press_fishing_button('Casting')
                        last_cast = time.monotonic()
                        recast_at = listening()
                    wait(config['CHECK_INTERVAL'])
            except PauseRequested:
                servo.set_neutral()
                paused_at = time.monotonic()
                if pause_event is not None:
                    pause_event.clear()
                on_pause()
                deadline += time.monotonic() - paused_at
                consecutive_failed_casts = 0
    except Stopped as exc:
        on_stage('stopping', None)
        log(str(exc))
    except Exception:
        on_stage('stopping', None)
        raise
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
