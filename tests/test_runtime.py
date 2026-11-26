import sys
import threading
import time
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, call, patch

import numpy as np
from configuration import DEFAULTS
from main import main, Stopped, PauseRequested
from servo import Servo


class RuntimeTests(unittest.TestCase):
    def run_fake(self, changes=None, stop_on_delay=None, read_error=False, stages=None,
                 stop_after_casts=2, audio_level=lambda: 8000, logs=None, pause=None, levels=None,
                 pause_at=None, recast_sample=lambda: 45, available_frames=1024):
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
        stream.get_read_available.return_value = available_frames
        stream.read.side_effect = RuntimeError('Audio read failed') if read_error else lambda *a, **k: np.full(1024,audio_level(),dtype=np.int16).tobytes()
        audio = Mock()
        audio.open.return_value = stream
        audio_module = SimpleNamespace(PyAudio=lambda: audio, paInt16=8)
        actions = []
        pause_event = threading.Event() if pause_at else None
        pause_requested = False
        def on_stage(name, duration):
            nonlocal pause_requested
            if stages is not None:
                stages.append((name, duration, now[0]))
            if name == pause_at and not pause_requested:
                pause_event.set()
                pause_requested = True
        servo = Mock()
        def factory(config, runtime_wait, log):
            def press(action):
                runtime_wait(0)
                actions.append(action)
                runtime_wait(config['SERVO_HOLD_TIME'])
                runtime_wait(config['SERVO_HOLD_TIME'])
                if stop_after_casts is not None and actions.count('Casting') >= stop_after_casts:
                    stopped[0] = True
            servo.press_fishing_button.side_effect = press
            return servo
        with patch('main.time.monotonic', side_effect=lambda: now[0]), patch('main.nd.for_config', side_effect=lambda config, prefix, mean=None: SimpleNamespace(rvs=recast_sample if prefix == 'RECAST_TIME' else lambda: 1.2)), patch('main.STATS_PATH'), patch('main.np.random.uniform', return_value=10):
            if read_error:
                with self.assertRaisesRegex(RuntimeError, 'Audio read failed'):
                    main(config, event, lambda _: None, audio_module=audio_module, servo_factory=factory, on_stage=on_stage)
            else:
                main(config, event, logs.append if logs is not None else lambda _: None,
                     audio_module=audio_module, servo_factory=factory, on_stage=on_stage,
                     on_pause=(lambda: pause(now, stopped, actions)) if pause else None,
                     on_audio=levels.append if levels is not None else lambda _: None,
                     pause_event=pause_event)
        servo.cleanup.assert_called_once()
        stream.close.assert_called_once()
        audio.terminate.assert_called_once()
        return actions, now[0]

    def test_start_and_automatic_resume_wait_three_seconds_before_casting(self):
        stages, resumed_at = [], []
        def pause(now, stopped, actions):
            now[0] += 100
            resumed_at.append(now[0])
        self.run_fake({}, stop_after_casts=6, audio_level=lambda: 0,
                      pause=pause, stages=stages)
        casts = [stage[2] for stage in stages if stage[0] == 'casting']
        self.assertEqual(casts[0], 3)
        self.assertAlmostEqual(casts[5] - resumed_at[0], 3)
        self.assertEqual(len([stage for stage in stages if stage[0] == 'startdelay']), 2)

    def test_recast_samples_once_per_cast_and_waits_only_the_remaining_time(self):
        stages, levels, logs = [], [], []
        samples = Mock(side_effect=[35, 50, 45])
        def noise():
            self.assertEqual(stages[-1][0], 'listening')
            cast = next(stage for stage in reversed(stages) if stage[0] == 'casting')
            return 8000 if time.monotonic() - cast[2] - cast[1] >= 30 else 0
        actions, _ = self.run_fake(stages=stages, levels=levels, logs=logs,
                                   audio_level=noise, recast_sample=samples, stop_after_casts=3)
        self.assertEqual(actions, ['Casting'] * 3)
        self.assertEqual(samples.call_count, 3)
        self.assertEqual(logs.count('Did not cast, recasting...'), 2)
        self.assertFalse(any(line.startswith('Audio level above threshold:') for line in logs))
        casts = [stage for stage in stages if stage[0] == 'casting']
        waits = [stage for stage in stages if stage[0] == 'reconciling']
        for index, timeout in enumerate((35, 50)):
            cast_end = casts[index][2] + casts[index][1]
            self.assertAlmostEqual(casts[index + 1][2] - cast_end, timeout)
            self.assertAlmostEqual(waits[index][2] + waits[index][1], cast_end + timeout)

    def test_reconciliation_wait_can_be_paused_stopped_or_hit_runtime_limit(self):
        stages, pauses = [], []
        def pause(now, stopped, actions):
            pauses.append(now[0])
        actions, _ = self.run_fake(audio_level=lambda: 0, stages=stages,
                                   pause=pause, pause_at='reconciling')
        self.assertEqual(len(pauses), 1)
        self.assertEqual(actions, ['Casting'] * 2)
        resumed_cast = [stage for stage in stages if stage[0] == 'casting'][1]
        self.assertAlmostEqual(resumed_cast[2] - pauses[0], 3)
        stages.clear()
        actions, _ = self.run_fake(audio_level=lambda: 0, stages=stages,
                                   stop_on_delay=lambda d: d > 0 and stages[-1][0] == 'reconciling')
        self.assertEqual(actions, ['Casting'])
        logs = []
        actions, elapsed = self.run_fake({'MAX_RUN_TIME': 40}, audio_level=lambda: 0, logs=logs)
        self.assertEqual(actions, ['Casting'])
        self.assertAlmostEqual(elapsed, 40)
        self.assertIn('Maximum runtime reached.', logs)

    def test_missing_audio_frames_still_reach_reconciliation(self):
        stages, logs = [], []
        actions, _ = self.run_fake(stages=stages, logs=logs, available_frames=0)
        self.assertEqual(actions, ['Casting'] * 2)
        self.assertEqual(logs.count('Did not cast, recasting...'), 1)
        self.assertIn('reconciling', [stage[0] for stage in stages])

    def test_wait_audio_is_not_processed_and_buffered_noise_is_discarded(self):
        for cutoff in (1.2, 2.1):
            with self.subTest(cutoff=cutoff):
                stages, logs, noisy_waits = [], [], set()
                pending_noise = [False]
                def during_wait(duration):
                    if duration > 0 and stages[-1][0] in (
                            'startdelay', 'reaction', 'skipping', 'idle', 'castdelay', 'reconciling'):
                        pending_noise[0] = True
                        noisy_waits.add(stages[-1][0])
                    return False
                def audio_level():
                    self.assertEqual(stages[-1][0], 'listening')
                    if pending_noise[0]:
                        pending_noise[0] = False
                        return 12000
                    return 8000 if not any(line.startswith('Audio level above threshold:') for line in logs) else 0
                self.run_fake({'DONT_REEL_TIME': cutoff, 'IDLE_PROBABILITY': 1},
                              stages=stages, logs=logs, audio_level=audio_level,
                              stop_on_delay=during_wait, stop_after_casts=3)
                self.assertEqual(sum(line.startswith('Audio level above threshold:') for line in logs), 1)
                expected = {'startdelay', 'reaction', 'idle', 'castdelay', 'reconciling'}
                if cutoff == 1.2:
                    expected.add('skipping')
                self.assertEqual(noisy_waits, expected)

    def test_manual_pause_interrupts_each_activity_and_resumes_with_a_fresh_cast(self):
        for activity in ('startdelay', 'casting', 'listening', 'reaction', 'reeling',
                         'skipping', 'idle', 'castdelay'):
            with self.subTest(activity=activity):
                stages, resumed_at, counts = [], [], []
                def pause(now, stopped, actions):
                    counts.append(len(actions))
                    now[0] += 100
                    resumed_at.append(now[0])
                actions, _ = self.run_fake(
                    {'IDLE_PROBABILITY': 1, 'DONT_REEL_TIME': 1.2 if activity == 'skipping' else 2.1},
                    pause=pause, pause_at=activity, stages=stages)
                self.assertEqual(len(resumed_at), 1)
                resume_cast = next(stage[2] for stage in stages
                                   if stage[0] == 'casting' and stage[2] >= resumed_at[0])
                self.assertAlmostEqual(resume_cast - resumed_at[0], 3)
                self.assertEqual(actions[counts[0]], 'Casting')

    def test_stop_during_start_or_resume_countdown_never_presses_a_key(self):
        self.assertEqual(self.run_fake(stop_on_delay=lambda duration: duration == 3)[0], [])
        resumed = []
        def pause(now, stopped, actions):
            resumed.append(True)
        actions, _ = self.run_fake(pause=pause, pause_at='startdelay',
                                   stop_on_delay=lambda duration: bool(resumed) and duration > 0)
        self.assertEqual(actions, [])

    def test_audio_reporting_uses_detection_level_without_changing_actions(self):
        for sample in (0, -8000, -32768):
            with self.subTest(sample=sample):
                levels = []
                expected = self.run_fake(audio_level=lambda: sample)
                actual = self.run_fake(audio_level=lambda: sample, levels=levels)
                self.assertEqual(actual, expected)
                self.assertTrue(levels)
                self.assertEqual(set(levels), {abs(sample)})

    def test_cast_guard_ignores_splashes_on_first_cast_and_timeout_recasts(self):
        for report_audio in (False, True):
            with self.subTest(report_audio=report_audio):
                stages, levels, logs = [], [], []
                def splash():
                    cast = [stage for stage in stages if stage[0] == 'casting'][-1]
                    return 8000 if time.monotonic() - cast[2] < 1 else 0
                actions, _ = self.run_fake(
                    {'REEL_TIME_DELAY_AFTER_CAST': 1.5},
                    stop_after_casts=3, audio_level=splash, stages=stages, logs=logs,
                    levels=levels if report_audio else None)
                self.assertEqual(actions, ['Casting'] * 3)
                self.assertFalse(any(stage[0] == 'reaction' for stage in stages))
                self.assertFalse(any(line.startswith('Audio level above threshold:') for line in logs))
                if report_audio:
                    self.assertIn(8000, levels)
                    self.assertIn(0, levels)

    def test_cast_guard_accepts_audio_only_after_delay_with_graph_enabled(self):
        stages, levels = [], []
        actions, _ = self.run_fake(
            {'REEL_TIME_DELAY_AFTER_CAST': 1.5}, stop_after_casts=3,
            stages=stages, levels=levels)
        casts = [stage for stage in stages if stage[0] == 'casting']
        reactions = [stage for stage in stages if stage[0] == 'reaction']
        self.assertEqual(actions, ['Casting', 'Reeling', 'Casting', 'Reeling', 'Casting'])
        self.assertEqual(len(reactions), 2)
        for index, reaction in enumerate(reactions):
            cast_timestamp = casts[index][2] + casts[index][1]
            self.assertGreater(reaction[2] - cast_timestamp, 1.5)
            self.assertLessEqual(reaction[2] - cast_timestamp, 1.53)
        self.assertGreater(len(levels), 2)

    def test_first_cast_guard_starts_after_servo_action_finishes(self):
        stages = []
        self.run_fake({'REEL_TIME_DELAY_AFTER_CAST': 1, 'SERVO_HOLD_TIME': .75},
                      stages=stages, levels=[])
        reaction = next(stage for stage in stages if stage[0] == 'reaction')
        self.assertGreater(reaction[2], 5.5)
        self.assertLessEqual(reaction[2], 5.53)

    def test_resume_resets_guard_and_stop_while_paused_does_not_cast(self):
        pauses, logs = [], []
        def pause(now, stopped, actions):
            pauses.append(actions.count('Casting'))
            now[0] += 100
            stopped[0] = len(pauses) == 3
        actions, _ = self.run_fake(
            {'MAX_RUN_TIME': 800}, stop_after_casts=None,
            audio_level=lambda: 0, logs=logs, pause=pause)
        self.assertEqual(pauses, [5, 10, 15])
        self.assertEqual(actions, ['Casting'] * 15)
        self.assertIn('Stop requested.', logs)

    def test_resume_preserves_remaining_runtime_excluding_paused_time(self):
        pauses, logs = [], []
        def pause(now, stopped, actions):
            pauses.append(now[0])
            now[0] += 100
        _, elapsed = self.run_fake(
            {'MAX_RUN_TIME': 250}, stop_after_casts=None,
            audio_level=lambda: 0, logs=logs, pause=pause)
        self.assertEqual(len(pauses), 1)
        self.assertAlmostEqual(elapsed, 350)
        self.assertIn('Maximum runtime reached.', logs)

    def test_five_consecutive_failed_casts_stop_and_new_session_resets_count(self):
        for _ in range(2):
            logs, stages = [], []
            actions, _ = self.run_fake(
                {'MAX_RUN_TIME': 800}, stop_after_casts=None,
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
                    {'MAX_RUN_TIME': 1200, 'DONT_REEL_TIME': cutoff},
                    stop_after_casts=None, audio_level=audio_level, logs=logs)
                self.assertEqual(logs.count('Did not cast, recasting...'), 9)
                self.assertEqual(actions.count('Casting'), 10)
                self.assertEqual(actions.count('Reeling'), int(cutoff > 1.2))
                self.assertIn('Stopped after 5 consecutive failed casts.', logs)

    def test_stop_during_reel_and_idle_does_not_cast_again(self):
        actions, _ = self.run_fake(stop_on_delay=lambda d: 1 <= d < 3)
        self.assertEqual(actions, ['Casting'])
        actions, _ = self.run_fake({'IDLE_PROBABILITY':1}, stop_on_delay=lambda d: d >= 10)
        self.assertEqual(actions, ['Casting','Reeling'])

    def test_hard_cutoff_and_elapsed_channel_duration(self):
        self.assertNotIn('Reeling', self.run_fake({'DONT_REEL_TIME':1.2})[0])
        self.assertNotIn('Reeling', self.run_fake({'WOW_FISHING_TIME':1})[0])
        self.assertIn('Reeling', self.run_fake({'WOW_FISHING_TIME':2})[0])

    def test_late_bite_does_not_count_reaction_delay_twice(self):
        actions, _ = self.run_fake({'WOW_FISHING_TIME': 28},
                                   audio_level=lambda: 8000 if time.monotonic() >= 26 else 0)
        self.assertEqual(actions, ['Casting', 'Reeling', 'Casting'])

    def test_hard_cutoff_waits_out_channel_before_cast_delay(self):
        stages, logs = [], []
        actions, _ = self.run_fake({'DONT_REEL_TIME': 1.2}, stages=stages, logs=logs,
                                   stop_after_casts=3)
        self.assertEqual(actions, ['Casting'] * 3)
        casts = [stage for stage in stages if stage[0] == 'casting']
        delays = [stage for stage in stages if stage[0] == 'castdelay']
        for cast, delay in zip(casts, delays):
            self.assertAlmostEqual(delay[2], cast[2] + cast[1] + 30)
        self.assertEqual(sum(line.startswith('Skipping reel: hard delay cutoff reached.') for line in logs), 2)
        actions, _ = self.run_fake({'DONT_REEL_TIME': 1.2}, stop_on_delay=lambda d: d > 5)
        self.assertEqual(actions, ['Casting'])

    def test_expired_channel_has_separate_reason_and_no_remaining_wait(self):
        stages, logs = [], []
        self.run_fake(audio_level=lambda: 8000 if time.monotonic() >= 32.5 else 0,
                      stages=stages, logs=logs)
        self.assertIn('Skipping reel: configured fishing channel duration elapsed.', logs)
        self.assertEqual(next(stage[1] for stage in stages if stage[0] == 'skipping'), 0)

    def test_deadline_interrupts_delay(self):
        actions, elapsed = self.run_fake({'MAX_RUN_TIME':1})
        self.assertEqual(actions,[])
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
            servo = Servo(dict(DEFAULTS, SERVO_RETURN=1500), wait, lambda _:None)
            with self.assertRaises(Stopped):
                servo.press_fishing_button('Casting')
            self.assertEqual(pi.set_servo_pulsewidth.call_args.args,(27,0))
            servo.cleanup()
            pi.stop.assert_called_once()

    def test_pause_during_servo_press_disables_pulses(self):
        pi = Mock(connected=True)
        pi.set_servo_pulsewidth.return_value = 0
        def wait(duration):
            if duration:
                raise PauseRequested()
        with patch.dict(sys.modules, {'pigpio': SimpleNamespace(pi=lambda: pi)}):
            servo = Servo(dict(DEFAULTS, SERVO_RETURN=1500), wait, lambda _: None)
            with self.assertRaises(PauseRequested):
                servo.press_fishing_button('Casting')
            self.assertEqual(pi.set_servo_pulsewidth.call_args.args, (27, 0))
            servo.cleanup()

    def test_servo_press_returns_then_disables_pulses(self):
        for return_pulse in (0, 1500):
            with self.subTest(return_pulse=return_pulse):
                pi = Mock(connected=True)
                pi.set_servo_pulsewidth.return_value = 0
                events = Mock()
                events.attach_mock(pi.set_servo_pulsewidth, 'pulse')
                with patch.dict(sys.modules, {'pigpio': SimpleNamespace(pi=lambda: pi)}):
                    servo = Servo(dict(DEFAULTS, SERVO_RETURN=return_pulse), events.wait, lambda _: None)
                    servo.press_fishing_button('Casting')
                self.assertEqual(events.mock_calls, [
                    call.wait(0), call.pulse(27, DEFAULTS['SERVO_CLICK']), call.wait(.1),
                    call.pulse(27, return_pulse), call.wait(.1), call.pulse(27, 0)])

    def test_servo_return_interruption_or_failure_disables_pulses(self):
        for failure in ('stop', 'error_code', 'exception'):
            with self.subTest(failure=failure):
                pi = Mock(connected=True)
                pi.set_servo_pulsewidth.side_effect = [
                    0, RuntimeError('Connection lost') if failure == 'exception' else
                    -1 if failure == 'error_code' else 0, 0]
                wait = Mock(side_effect=[None, None, Stopped('Stop requested')])
                with patch.dict(sys.modules, {'pigpio': SimpleNamespace(pi=lambda: pi)}):
                    servo = Servo(dict(DEFAULTS, SERVO_RETURN=1500), wait, lambda _: None)
                    with self.assertRaises(Stopped if failure == 'stop' else RuntimeError):
                        servo.press_fishing_button('Casting')
                self.assertEqual(pi.set_servo_pulsewidth.call_args_list,
                                 [call(27, DEFAULTS['SERVO_CLICK']), call(27, 1500), call(27, 0)])

    def test_stages_follow_actions_and_actual_sampled_waits(self):
        stages = []
        self.run_fake({'SERVO_HOLD_TIME': .25}, stages=stages)
        self.assertEqual(stages.pop(0), ('startdelay', 3, 0))
        self.assertEqual([name for name, _, _ in stages],
                         ['casting', 'listening', 'reaction', 'reeling', 'castdelay', 'casting', 'listening', 'stopping'])
        self.assertEqual(stages[0][:2], ('casting', .5))
        self.assertEqual(stages[1][:2], ('listening', DEFAULTS['WOW_FISHING_TIME']))
        self.assertEqual(stages[2][:2], ('reaction', 1.2))
        self.assertEqual(stages[3][:2], ('reeling', .5))
        self.assertEqual(stages[4][:2], ('castdelay', 1.2))
        for current, following in zip(stages[2:6], stages[3:7]):
            self.assertAlmostEqual(following[2] - current[2], current[1])
        self.assertAlmostEqual(stages[6][1], DEFAULTS['WOW_FISHING_TIME'])

    def test_skip_idle_timeout_and_interruption_stages(self):
        stages = []
        self.run_fake({'DONT_REEL_TIME': 1.2, 'IDLE_PROBABILITY': 1}, stages=stages)
        names = [name for name, _, _ in stages]
        self.assertEqual(names[3:7], ['reaction', 'skipping', 'idle', 'castdelay'])
        skipped = stages[4]
        self.assertAlmostEqual(skipped[1], 3.2 + DEFAULTS['WOW_FISHING_TIME'] - skipped[2])
        self.assertEqual(stages[5][1], 10)
        stages.clear()
        self.run_fake({'DONT_REEL_TIME': 1.2, 'WOW_FISHING_TIME': 1}, stages=stages)
        self.assertEqual(next(duration for name, duration, _ in stages if name == 'skipping'), 0)
        stages.clear()
        self.run_fake({'THRESHOLD': 10000}, stages=stages)
        self.assertEqual([name for name, _, _ in stages], ['startdelay', 'casting', 'listening', 'reconciling', 'casting', 'listening', 'stopping'])
        stages.clear()
        self.run_fake(stop_on_delay=lambda d: 1 <= d < 3, stages=stages)
        self.assertEqual([name for name, _, _ in stages], ['startdelay', 'casting', 'listening', 'reaction', 'stopping'])
        stages.clear()
        self.run_fake({'MAX_RUN_TIME': 1}, stages=stages)
        self.assertEqual(stages[-1], ('stopping', None, 1))
        stages.clear()
        self.run_fake(read_error=True, stages=stages)
        self.assertEqual(stages[-1][0], 'stopping')


if __name__ == '__main__':
    unittest.main()
