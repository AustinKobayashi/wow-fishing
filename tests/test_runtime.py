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
    def run_fake(self, changes=None, stop_on_delay=None, read_error=False):
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
        stream.read.side_effect = RuntimeError('Audio read failed') if read_error else lambda *a, **k: np.full(1024,8000,dtype=np.int16).tobytes()
        audio = Mock()
        audio.open.return_value = stream
        audio_module = SimpleNamespace(PyAudio=lambda: audio, paInt16=8)
        actions = []
        servo = Mock()
        def factory(config, runtime_wait, log):
            def press(action):
                runtime_wait(0)
                actions.append(action)
                runtime_wait(config['SERVO_SPEED'])
                runtime_wait(config['SERVO_SPEED'])
                if actions.count('Casting') >= 2:
                    stopped[0] = True
            servo.press_fishing_button.side_effect = press
            return servo
        with patch('main.time.monotonic', side_effect=lambda: now[0]), patch('main.nd.for_config', return_value=SimpleNamespace(rvs=lambda:1.2)), patch('main.STATS_PATH'), patch('main.np.random.uniform', return_value=10):
            if read_error:
                with self.assertRaisesRegex(RuntimeError, 'Audio read failed'):
                    main(config, event, lambda _: None, audio_module=audio_module, servo_factory=factory)
            else:
                main(config, event, lambda _: None, audio_module=audio_module, servo_factory=factory)
        servo.cleanup.assert_called_once()
        stream.close.assert_called_once()
        audio.terminate.assert_called_once()
        return actions, now[0]

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


if __name__ == '__main__':
    unittest.main()
