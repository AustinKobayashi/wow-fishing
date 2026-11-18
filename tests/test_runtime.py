import sys
import threading
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

import numpy as np
from configuration import DEFAULTS
from main import main, Stopped
from servo import Servo


class RuntimeTests(unittest.TestCase):
    def run_fake(self, changes=None, stop_on_delay=None, read_error=False, stages=None,
                 stop_after_casts=2, audio_level=lambda: 8000, logs=None):
        config = dict(DEFAULTS, REEL_TIME_DELAY_AFTER_CAST=0, IDLE_PROBABILITY=0)
        config.update(changes or {})
        now = [0.0]
        stopped = [False]
        def wait(duration):
            if stopped[0] or (stop_on_delay is not None and stop_on_delay(duration)):
                stopped[0] = True
                return True
            now[0] += duration
            return False
        event = SimpleNamespace(wait=wait)
        stream = Mock()
        stream.is_active.return_value = True
        stream.get_read_available.return_value = 1024
        stream.read.side_effect = RuntimeError('Audio read failed') if read_error else lambda *a, **k: np.full(1024,audio_level(),dtype=np.int16).tobytes()
        audio = Mock()
        audio.open.return_value = stream
        audio_module = SimpleNamespace(PyAudio=lambda: audio, paInt16=8)
        actions = []
        def on_stage(name, duration):
            if stages is not None:
                stages.append((name, duration, now[0]))
        servo = Mock()
        def factory(config, runtime_wait, log):
            def press(action):
                runtime_wait(0)
                actions.append(action)
                runtime_wait(config['SERVO_SPEED'])
                runtime_wait(config['SERVO_SPEED'])
                if stop_after_casts is not None and actions.count('Casting') >= stop_after_casts:
                    stopped[0] = True
            servo.press_fishing_button.side_effect = press
            return servo
        with patch('main.time.monotonic', side_effect=lambda: now[0]), patch('main.nd.for_config', return_value=SimpleNamespace(rvs=lambda:1.2)), patch('main.STATS_PATH'), patch('main.np.random.uniform', return_value=10):
            if read_error:
                with self.assertRaisesRegex(RuntimeError, 'Audio read failed'):
                    main(config, event, lambda _: None, audio_module=audio_module, servo_factory=factory, on_stage=on_stage)
            else:
                main(config, event, logs.append if logs is not None else lambda _: None,
                     audio_module=audio_module, servo_factory=factory, on_stage=on_stage)
        servo.cleanup.assert_called_once()
        stream.close.assert_called_once()
        audio.terminate.assert_called_once()
        return actions, now[0]

    def test_five_consecutive_failed_casts_stop_and_new_session_resets_count(self):
        for _ in range(2):
            logs, stages = [], []
            actions, _ = self.run_fake(
                {'LAST_CAST_MAX': 1, 'MAX_RUN_TIME': 30}, stop_after_casts=None,
                audio_level=lambda: 0, logs=logs, stages=stages)
            self.assertEqual(logs.count('Did not cast, recasting...'), 5)
            self.assertEqual(actions, ['Casting'] * 5)
            self.assertIn('Stopped after 5 consecutive failed casts.', logs)
            self.assertEqual(stages[-1][:2], ('stopping', None))

    def test_detected_bite_resets_failed_cast_count_even_if_reel_is_skipped(self):
        for cutoff in (2.1, 1.2):
            with self.subTest(cutoff=cutoff):
                logs = []
                def audio_level():
                    return 8000 if (logs.count('Did not cast, recasting...') == 4
                                    and not any(line.startswith('Audio level above threshold:') for line in logs)) else 0
                actions, _ = self.run_fake(
                    {'LAST_CAST_MAX': 1, 'MAX_RUN_TIME': 120, 'DONT_REEL_TIME': cutoff},
                    stop_after_casts=None, audio_level=audio_level, logs=logs)
                self.assertEqual(logs.count('Did not cast, recasting...'), 9)
                self.assertEqual(actions.count('Casting'), 10)
                self.assertEqual(actions.count('Reeling'), int(cutoff > 1.2))
                self.assertIn('Stopped after 5 consecutive failed casts.', logs)

    def test_stop_during_reel_and_idle_does_not_cast_again(self):
        actions, _ = self.run_fake(stop_on_delay=lambda d: d >= 1)
        self.assertEqual(actions, ['Casting'])
        actions, _ = self.run_fake({'IDLE_PROBABILITY':1}, stop_on_delay=lambda d: d >= 10)
        self.assertEqual(actions, ['Casting','Reeling'])

    def test_hard_cutoff_and_existing_double_delay_rule(self):
        self.assertNotIn('Reeling', self.run_fake({'DONT_REEL_TIME':1.2})[0])
        self.assertNotIn('Reeling', self.run_fake({'WOW_FISHING_TIME':2})[0])
        self.assertIn('Reeling', self.run_fake({'WOW_FISHING_TIME':4})[0])

    def test_deadline_interrupts_delay(self):
        actions, elapsed = self.run_fake({'MAX_RUN_TIME':1})
        self.assertEqual(actions,['Casting'])
        self.assertAlmostEqual(elapsed,1)

    def test_audio_failure_cleans_up_all_resources(self):
        self.run_fake(read_error=True)
        audio = Mock()
        audio.open.side_effect = OSError('Invalid sample rate')
        with self.assertRaisesRegex(OSError,'Invalid sample rate'):
            main(DEFAULTS, threading.Event(), lambda _:None,
                 audio_module=SimpleNamespace(PyAudio=lambda:audio, paInt16=8))
        audio.terminate.assert_called_once()

    def test_stop_during_servo_press_disables_pulses(self):
        pi = Mock(connected=True)
        pi.set_servo_pulsewidth.return_value = 0
        def wait(duration):
            if duration:
                raise Stopped('Stop requested')
        with patch.dict(sys.modules, {'pigpio': SimpleNamespace(pi=lambda:pi)}):
            servo = Servo(DEFAULTS, wait, lambda _:None)
            with self.assertRaises(Stopped):
                servo.press_fishing_button('Casting')
            self.assertEqual(pi.set_servo_pulsewidth.call_args.args,(27,0))
            servo.cleanup()
            pi.stop.assert_called_once()

    def test_stages_follow_actions_and_actual_sampled_waits(self):
        stages = []
        self.run_fake({'SERVO_SPEED': .25}, stages=stages)
        self.assertEqual([name for name, _, _ in stages],
                         ['casting', 'listening', 'reaction', 'reeling', 'castdelay', 'casting', 'listening', 'stopping'])
        self.assertEqual(stages[0][:2], ('casting', .5))
        self.assertEqual(stages[1][:2], ('listening', DEFAULTS['LAST_CAST_MAX'] - .5))
        self.assertEqual(stages[2][:2], ('reaction', 1.2))
        self.assertEqual(stages[3][:2], ('reeling', .5))
        self.assertEqual(stages[4][:2], ('castdelay', 1.2))
        for current, following in zip(stages[2:6], stages[3:7]):
            self.assertAlmostEqual(following[2] - current[2], current[1])
        self.assertEqual(stages[6][1], DEFAULTS['LAST_CAST_MAX'])

    def test_skip_idle_timeout_and_interruption_stages(self):
        stages = []
        self.run_fake({'DONT_REEL_TIME': 1.2, 'IDLE_PROBABILITY': 1}, stages=stages)
        names = [name for name, _, _ in stages]
        self.assertEqual(names[2:6], ['reaction', 'skipping', 'idle', 'castdelay'])
        skipped = stages[3]
        self.assertAlmostEqual(skipped[1], DEFAULTS['WOW_FISHING_TIME'] - skipped[2])
        self.assertEqual(stages[4][1], 10)
        stages.clear()
        self.run_fake({'DONT_REEL_TIME': 1.2, 'WOW_FISHING_TIME': 1}, stages=stages)
        self.assertEqual(next(duration for name, duration, _ in stages if name == 'skipping'), .3)
        stages.clear()
        self.run_fake({'THRESHOLD': 10000, 'LAST_CAST_MAX': 1}, stages=stages)
        self.assertEqual([name for name, _, _ in stages], ['casting', 'listening', 'casting', 'listening', 'stopping'])
        stages.clear()
        self.run_fake(stop_on_delay=lambda d: d >= 1, stages=stages)
        self.assertEqual([name for name, _, _ in stages], ['casting', 'listening', 'reaction', 'stopping'])
        stages.clear()
        self.run_fake({'MAX_RUN_TIME': 1}, stages=stages)
        self.assertEqual(stages[-1], ('stopping', None, 1))
        stages.clear()
        self.run_fake(read_error=True, stages=stages)
        self.assertEqual(stages[-1][0], 'stopping')


if __name__ == '__main__':
    unittest.main()
