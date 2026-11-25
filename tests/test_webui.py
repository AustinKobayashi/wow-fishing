import json
import sys
from pathlib import Path
import tempfile
import threading
import time
import unittest
from unittest.mock import Mock, patch
from datetime import datetime
from urllib.error import HTTPError
from urllib.request import Request, urlopen

from configuration import DEFAULTS, load_config, validate
from normal_distribution import for_config, preview
from webui import Conflict, Controller, Handler, Server
import webui


class ConfigAndMathTests(unittest.TestCase):
    def test_invalid_numbers_and_unknown_fields(self):
        for value in (True, '7000', float('nan'), float('inf'), -1):
            with self.subTest(value=value), self.assertRaises(ValueError):
                validate(dict(DEFAULTS, THRESHOLD=value))
        with self.assertRaises(ValueError):
            validate(dict(DEFAULTS, SERVO_PIN=12))
        with self.assertRaises(ValueError):
            validate(dict(DEFAULTS, RATE=48000.1))
        with self.assertRaises(ValueError):
            validate(dict(DEFAULTS, IDLE_TIME_MIN=121))
        with self.assertRaises(ValueError):
            validate(dict(DEFAULTS, REEL_TIME_MIN=4))

    def test_cutoff_and_window_probabilities(self):
        data = preview(DEFAULTS, 'reel', 25)
        self.assertAlmostEqual(data['cutoff_skip'], .040894501238410166, places=10)
        self.assertAlmostEqual(data['combined_skip'], .4150702510562751, places=10)
        self.assertEqual(data['effective_limit'], 1.5)
        self.assertTrue(any(x == DEFAULTS['DONT_REEL_TIME'] for x, _ in data['points']))
        self.assertEqual(preview(dict(DEFAULTS, DONT_REEL_TIME=.5), 'reel', 10)['cutoff_skip'], 1)
        self.assertTrue(preview(DEFAULTS, 'reel', 1.5)['guard_blocks'])
        self.assertTrue(preview(DEFAULTS, 'reel', 61)['recast_due'])

    def test_cast_conditional_matches_runtime_and_density_integrates(self):
        from scipy.integrate import quad
        for prefix in ('REEL_TIME', 'CAST_TIME'):
            mean = 1.4 if prefix == 'CAST_TIME' else None
            model = for_config(DEFAULTS, prefix, mean)
            area = quad(model.pdf, DEFAULTS[prefix + '_MIN'], 10)[0]
            self.assertAlmostEqual(area, 1, places=9)
            self.assertTrue((model.rvs(size=1000, random_state=42) >= DEFAULTS[prefix + '_MIN']).all())
        data = preview(DEFAULTS, 'cast', 1.4)
        self.assertEqual(data['mu'], 1.4)
        self.assertAlmostEqual(data['mean'], for_config(DEFAULTS, 'CAST_TIME', 1.4).mean())
        self.assertGreater(data['above_reference'], 0)
        with self.assertRaises(ValueError):
            preview(DEFAULTS, 'cast', .5)


class ServerTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.calls = []
        def runner(config, stop_event, log, on_ready, on_stage, on_pause, on_audio):
            self.calls.append(config)
            on_ready()
            on_stage('listening', config['LAST_CAST_MAX'])
            on_audio(1234)
            log('Test runner is ready')
            stop_event.wait(3)
            log('Test runner cleaned up')
        self.controller = Controller(Path(self.temp.name) / 'config.json', runner, log_sink=lambda _: None)
        self.server = Server(0, self.controller)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.base = 'http://127.0.0.1:' + str(self.server.server_port)

    def tearDown(self):
        self.controller.close()
        self.server.shutdown()
        self.server.server_close()
        self.thread.join()
        self.temp.cleanup()

    def request(self, path, payload=None, headers=None):
        req_headers = {'Content-Type': 'application/json', 'X-Control-Token': self.server.token}
        req_headers.update(headers or {})
        request = Request(self.base + path, data=json.dumps(payload).encode() if payload is not None else None,
                          headers=req_headers)
        try:
            response = urlopen(request, timeout=3)
        except HTTPError as exc:
            response = exc
        with response:
            raw = response.read()
            return response.status, json.loads(raw) if response.headers.get_content_type() == 'application/json' else raw

    def test_pause_resume_keeps_session_and_timer_and_stop_ends_it(self):
        now = [100.0]
        resumes = []
        def runner(on_ready, on_pause, stop_event, **_):
            self.calls.append('started')
            on_ready()
            now[0] = 110.0
            on_pause()
            if not stop_event.is_set():
                resumes.append(True)
                now[0] = 1015.0
                on_pause()
        self.controller.runner = runner
        with patch('webui.time.monotonic', side_effect=lambda: now[0]):
            self.controller.start()
            worker = self.controller.worker
            with self.controller.stage_condition:
                self.assertTrue(self.controller.stage_condition.wait_for(
                    lambda: self.controller.status == 'Paused', timeout=2))
            now[0] = 1000.0
            state = self.request('/api/state')[1]
            self.assertTrue(state['active'])
            self.assertEqual(state['status'], 'Paused')
            self.assertEqual(state['stage']['name'], 'paused')
            self.assertEqual(state['elapsed'], 10)
            self.assertEqual(self.request('/api/config', {'config': DEFAULTS})[0], 409)
            self.assertEqual(self.request('/api/reconnect-microphone', {})[0], 409)
            self.assertEqual(self.request('/api/start', {})[0], 200)
            with self.controller.stage_condition:
                self.assertTrue(self.controller.stage_condition.wait_for(
                    lambda: self.controller.status == 'Paused', timeout=2))
            self.assertIs(self.controller.worker, worker)
            self.assertEqual(self.calls, ['started'])
            self.assertEqual(resumes, [True])
            now[0] = 2000.0
            self.assertEqual(self.controller.snapshot()['elapsed'], 25)
            self.assertEqual(self.request('/api/stop', {})[0], 200)
            worker.join(timeout=2)
            state = self.controller.snapshot()
            self.assertFalse(state['active'])
            self.assertEqual(state['status'], 'Stopped')
            self.assertEqual(state['elapsed'], 25)

    def test_shutdown_wakes_paused_session_for_cleanup(self):
        cleaned = threading.Event()
        def runner(on_pause, **_):
            on_pause()
            cleaned.set()
        self.controller.runner = runner
        self.controller.start()
        with self.controller.stage_condition:
            self.assertTrue(self.controller.stage_condition.wait_for(
                lambda: self.controller.status == 'Paused', timeout=2))
        self.controller.close()
        self.assertTrue(cleaned.is_set())
        self.assertFalse(self.controller.active())

    def test_microphone_restart_protection_and_active_session_conflict(self):
        for headers in ({'X-Control-Token': 'wrong'}, {'Origin': 'http://foreign.example'},
                        {'Host': 'foreign.example'}):
            self.assertEqual(self.request('/api/reconnect-microphone', {}, headers)[0], 403)
        self.controller.start()
        self.assertEqual(self.request('/api/reconnect-microphone', {})[0], 409)
        self.assertFalse(self.server.restart_requested)
        self.assertFalse(self.controller.closing)

    def test_microphone_restart_preserves_data_and_blocks_new_work(self):
        self.controller.save(dict(DEFAULTS, RATE=48000))
        self.controller.create_catch_log(3600, 'Stormwind')
        self.controller.add_catch(0, 'Catfish', 2)
        config = self.controller.config_path.read_bytes()
        catches = self.controller.catches_path.read_bytes()
        self.controller.status, self.controller.error = 'Error', 'Invalid input device'
        status, state = self.request('/api/reconnect-microphone', {})
        self.assertEqual(status, 202)
        self.assertEqual(state['status'], 'Restarting')
        self.assertFalse(state['active'])
        self.assertTrue(self.server.restart_requested)
        self.thread.join(timeout=2)
        self.assertFalse(self.thread.is_alive())
        with self.assertRaises(Conflict):
            self.controller.start()
        with self.assertRaises(Conflict):
            self.controller.save(DEFAULTS)
        with self.assertRaises(Conflict):
            self.controller.create_catch_log(1, 'Stormwind')
        with self.assertRaises(Conflict):
            self.server.request_restart()
        self.assertEqual(self.calls, [])
        self.assertEqual(self.controller.config_path.read_bytes(), config)
        self.assertEqual(self.controller.catches_path.read_bytes(), catches)

    def test_microphone_check_uses_saved_settings_and_closes_on_success_or_failure(self):
        self.controller.config.update(RATE=48000, CHANNELS=1)
        for failure in (None, 'open', 'read', 'short', 'close'):
            with self.subTest(failure=failure):
                module = Mock(paInt16=8)
                audio = module.PyAudio.return_value
                stream = audio.open.return_value
                audio.get_default_input_device_info.return_value = {'name': 'USB test mic'}
                stream.read.return_value = b'\0' * (1 if failure == 'short' else 2048)
                if failure == 'open':
                    audio.open.side_effect = OSError('unavailable')
                if failure == 'read':
                    stream.read.side_effect = OSError('read failed')
                if failure == 'close':
                    stream.close.side_effect = OSError('close failed')
                with patch.dict(sys.modules, pyaudio=module):
                    if failure:
                        with self.assertRaises(OSError):
                            self.controller.check_microphone()
                    else:
                        self.controller.check_microphone()
                        self.assertIn('USB test mic at 48000 Hz', self.controller.microphone)
                audio.open.assert_called_once_with(format=8, channels=1, rate=48000,
                                                   input=True, frames_per_buffer=1024)
                audio.terminate.assert_called_once()
                if failure != 'open':
                    stream.close.assert_called_once()
                self.assertEqual(self.calls, [])
                self.assertFalse(self.controller.active())

    def test_process_restart_preserves_arguments_and_checks_audio_without_starting(self):
        arguments = ['webui.py', '--port', '9876', '--allow-network', '192.168.1.0/24']
        for checking in (False, True):
            with self.subTest(checking=checking):
                argv = arguments + (['--check-microphone'] if checking else [])
                controller = Mock()
                server = Mock(restart_requested=True, server_address=('0.0.0.0', 9876), server_port=9876)
                controller.check_microphone.side_effect = OSError('USB mic missing')
                with patch.object(sys, 'argv', argv), patch('webui.Controller', return_value=controller), \
                     patch('webui.Server', return_value=server), patch('webui.signal.signal'), \
                     patch('webui.os.execv') as execute:
                    webui.main()
                execute.assert_called_once_with(sys.executable,
                    [sys.executable, *arguments, '--check-microphone'])
                controller.start.assert_not_called()
                controller.close.assert_called_once()
                server.server_close.assert_called_once()
                if checking:
                    self.assertEqual(controller.status, 'Error')
                    self.assertIn('USB mic missing', controller.error)
                else:
                    controller.check_microphone.assert_not_called()

    def test_http_config_start_singleton_stop_and_restart(self):
        status, boot = self.request('/api/bootstrap')
        self.assertEqual(status, 200)
        self.assertEqual(boot['state']['status'], 'Stopped')
        changed = dict(DEFAULTS, SERVO_CLICK=1000, THRESHOLD=9000)
        self.assertEqual(self.request('/api/config', {'config': changed})[0], 200)
        self.assertEqual(load_config(self.controller.config_path), changed)
        self.assertEqual(self.request('/api/start', {})[0], 200)
        self.assertEqual(self.request('/api/start', {})[0], 409)
        self.assertEqual(self.request('/api/config', {'config': DEFAULTS})[0], 409)
        self.assertEqual(self.calls[0]['SERVO_CLICK'], 1000)
        self.assertFalse(self.controller.worker.daemon)
        self.assertEqual(self.request('/api/stop', {})[0], 200)
        self.controller.worker.join(2)
        self.assertFalse(self.controller.active())
        self.assertEqual(self.controller.snapshot()['status'], 'Stopped')
        self.assertEqual(self.request('/api/start', {})[0], 200)
        self.assertEqual(self.request('/api/stop', {})[0], 200)

    def test_api_validation_and_local_request_protection(self):
        for headers in ({'X-Control-Token': ''}, {'Origin': 'https://untrusted.example'}, {'Host': 'untrusted.example'}):
            self.assertEqual(self.request('/api/start', {}, headers)[0], 403)
        self.assertEqual(self.request('/api/config', {'config': dict(DEFAULTS, SERVO_CLICK=99999)})[0], 400)
        self.assertEqual(self.request('/api/preview', {'config': DEFAULTS, 'mode': 'reel', 'scenario': 25})[0], 200)
        self.assertEqual(self.request('/../main.py')[0], 404)
        self.assertEqual(self.request('/config.json')[0], 404)
        self.assertEqual(self.request('/')[0], 200)
        with urlopen(self.base + '/app.js') as response:
            self.assertEqual(response.headers.get_content_type(), 'text/javascript')
        self.assertEqual(self.request('/assets/wow-classic.png')[0], 200)

    def test_manual_catch_logs_persist_names_counts_location_and_date(self):
        self.assertEqual(self.request('/api/catches')[1], {
            'sessions': [], 'fishNames': [], 'ratesByLocation': {}, 'rates': {
                'seconds': 0, 'amount': 0, 'perHour': None, 'catches': [], 'stacks': 0, 'stacksPerHour': None}})
        status, data = self.request('/api/catches/new', {'sessionLength': 7200, 'location': ' Stormwind '})
        self.assertEqual(status, 200)
        session = data['sessions'][0]
        self.assertEqual(session['sessionLength'], 7200)
        self.assertEqual(session['location'], 'Stormwind')
        self.assertIsNotNone(datetime.fromisoformat(session['date']).tzinfo)
        self.assertEqual(session['catches'], [])
        for fish, amount in [('Bristle Whisker Catfish', 3), (' bristle whisker catfish ', 2), ('Raw Longjaw Mud Snapper', 4)]:
            status, data = self.request('/api/catches', {'session': 0, 'fish': fish, 'amount': amount})
            self.assertEqual(status, 200)
        self.assertEqual(data['sessions'][0]['catches'], [
            {'fish': 'Bristle Whisker Catfish', 'amount': 5, 'stacks': True},
            {'fish': 'Raw Longjaw Mud Snapper', 'amount': 4, 'stacks': True}])
        self.assertEqual(data['sessions'][0]['sessionLength'], 7200)
        self.assertEqual(data['sessions'][0]['date'], session['date'])
        self.request('/api/catches/new', {'sessionLength': 900, 'location': 'Goldshire'})
        status, data = self.request('/api/catches', {'session': 1, 'fish': 'BRISTLE WHISKER CATFISH', 'amount': 1})
        self.assertEqual(status, 200)
        self.assertEqual(data['sessions'][1]['catches'], [{'fish': 'Bristle Whisker Catfish', 'amount': 1, 'stacks': True}])
        restored = Controller(self.controller.config_path, log_sink=lambda _: None)
        self.assertEqual(restored.catch_log(), data)
        self.assertEqual(json.loads(self.controller.catches_path.read_text(encoding='utf-8')), data['sessions'])
        self.assertFalse(self.controller.active())
        self.assertEqual(self.calls, [])
        self.assertEqual(self.request('/catches.json')[0], 404)

    def test_catch_validation_and_request_protection(self):
        for duration in (True, -1, 1.5, '7200', None, 9007199254740992):
            self.assertEqual(self.request('/api/catches/new', {'sessionLength': duration, 'location': 'Stormwind'})[0], 400)
        for location in (' ', None, 123, 'x' * 121):
            self.assertEqual(self.request('/api/catches/new', {'sessionLength': 7200, 'location': location})[0], 400)
        self.assertEqual(self.request('/api/catches/new', {'sessionLength': 0, 'location': 'Stormwind'})[0], 200)
        payload = {'session': 0, 'fish': 'Catfish', 'amount': 1}
        for key, values in {'session': [-1, 1, True, '0', None], 'fish': ['', ' ', None, 1, 'x' * 121],
                            'amount': [0, -1, True, 1.5, '2', None, 9007199254740992],
                            'stacks': [0, 1, 'false', None, []]}.items():
            for value in values:
                with self.subTest(key=key, value=value):
                    self.assertEqual(self.request('/api/catches', dict(payload, **{key: value}))[0], 400)
        for path, body in [('/api/catches', payload), ('/api/catches/new', {'sessionLength': 1, 'location': 'Stormwind'})]:
            self.assertEqual(self.request(path, body, {'X-Control-Token': ''})[0], 403)
        self.assertEqual(self.controller.catch_log()['sessions'][0]['catches'], [])

    def test_catch_write_failure_preserves_saved_data(self):
        self.controller.create_catch_log(7200, 'Stormwind')
        before = self.controller.catch_log()
        disk = self.controller.catches_path.read_bytes()
        with patch.object(Path, 'replace', side_effect=OSError('Disk unavailable')):
            self.assertEqual(self.request('/api/catches', {'session': 0, 'fish': 'Catfish', 'amount': 1})[0], 500)
            self.assertEqual(self.request('/api/catches/new', {'sessionLength': 1, 'location': 'Goldshire'})[0], 500)
        self.assertEqual(self.controller.catch_log(), before)
        self.assertEqual(self.controller.catches_path.read_bytes(), disk)

    def test_edit_fish_row_preserves_session_details_other_rows_and_persists_rates(self):
        self.controller.create_catch_log(3600, 'Stormwind')
        self.controller.add_catch(0, 'Catfish', 25)
        self.controller.add_catch(0, 'Smallfish', 10)
        self.controller.create_catch_log(3600, 'Goldshire')
        self.controller.add_catch(1, 'Catfish', 35)
        original = self.controller.catch_log()['sessions']
        status, data = self.request('/api/catches/edit', {
            'session': 0, 'date': original[0]['date'], 'index': 0, 'original': original[0]['catches'][0],
            'fish': ' Rare fish ', 'amount': 3, 'stacks': False,
            'sessionLength': 7200, 'location': 'Must not change'})
        self.assertEqual(status, 200)
        self.assertEqual(data['sessions'][0], dict(original[0], catches=[
            {'fish': 'Rare fish', 'amount': 3, 'stacks': False}, original[0]['catches'][1]]))
        self.assertEqual(data['sessions'][1], original[1])
        self.assertEqual(data['rates']['perHour'], 24)
        self.assertEqual(data['rates']['stacksPerHour'], 3)
        restored = Controller(self.controller.config_path, log_sink=lambda _: None)
        self.assertEqual(restored.catch_log(), data)

    def test_edit_fish_row_merges_matching_names_and_flags_without_double_counting(self):
        self.controller.create_catch_log(3600, 'Stormwind')
        self.controller.add_catch(0, 'Catfish', 25)
        self.controller.add_catch(0, 'Rare fish', 3, False)
        session = self.controller.catch_log()['sessions'][0]
        identity = dict(session=0, date=session['date'], index=1, original=session['catches'][1])
        status, data = self.request('/api/catches/edit', dict(identity, fish=' catFISH ', amount=5, stacks=True))
        self.assertEqual(status, 200)
        self.assertEqual(data['sessions'][0]['catches'], [dict(fish='Catfish', amount=30, stacks=True)])
        self.assertEqual(data['rates']['perHour'], 30)
        self.assertEqual(data['rates']['stacksPerHour'], 2)
        # Legacy rows still edit without needing a stacks flag in their saved representation.
        self.controller.write_catches([dict(session, catches=[dict(fish='Catfish', amount=25)])])
        status, data = self.request('/api/catches/edit', dict(session=0, date=session['date'], index=0,
            original=dict(fish='Catfish', amount=25), fish='Catfish', amount=7, stacks=False))
        self.assertEqual(status, 200)
        self.assertEqual(data['sessions'][0]['catches'], [dict(fish='Catfish', amount=7, stacks=False)])
        self.assertEqual(data['rates']['stacksPerHour'], 7)

    def test_remove_fish_row_preserves_session_and_rejects_stale_rows(self):
        self.controller.create_catch_log(3600, 'Stormwind')
        self.controller.add_catch(0, 'Catfish', 25)
        self.controller.add_catch(0, 'Smallfish', 10)
        session = self.controller.catch_log()['sessions'][0]
        identity = dict(session=0, date=session['date'], index=0, original=session['catches'][0])
        self.controller.add_catch(0, 'Catfish', 1)
        edit = dict(identity, fish='Catfish', amount=1, stacks=True)
        self.assertEqual(self.request('/api/catches/edit', edit)[0], 409)
        self.assertEqual(self.request('/api/catches/remove', identity)[0], 409)
        identity['original'] = self.controller.catch_log()['sessions'][0]['catches'][0]
        status, data = self.request('/api/catches/remove', identity)
        self.assertEqual(status, 200)
        self.assertEqual(data['sessions'][0], dict(session, catches=[session['catches'][1]]))
        self.assertEqual(data['rates']['perHour'], 10)
        self.assertEqual(data['rates']['stacksPerHour'], 1)
        self.assertEqual(self.request('/api/catches/remove', identity)[0], 409)
        identity['original'] = session['catches'][1]
        status, data = self.request('/api/catches/remove', identity)
        self.assertEqual(status, 200)
        self.assertEqual(data['sessions'][0], dict(session, catches=[]))
        self.assertEqual(data['rates']['perHour'], 0)
        self.assertEqual(Controller(self.controller.config_path, log_sink=lambda _: None).catch_log(), data)

    def test_delete_session_persists_recalculates_and_rejects_stale_selection(self):
        self.controller.create_catch_log(3600, 'Stormwind')
        self.controller.add_catch(0, 'Catfish', 25)
        self.controller.create_catch_log(3600, 'Goldshire')
        self.controller.add_catch(1, 'Catfish', 35)
        original = self.controller.catch_log()['sessions']
        identity = dict(session=0, date=original[0]['date'], index=0, original=original[0]['catches'][0])
        status, data = self.request('/api/catches/delete', identity)
        self.assertEqual(status, 200)
        self.assertEqual(data['sessions'], [original[1]])
        self.assertEqual(data['rates']['perHour'], 35)
        self.assertEqual(data['rates']['stacksPerHour'], 2)
        for path in ('/api/catches/delete', '/api/catches/edit', '/api/catches/remove', '/api/catches'):
            self.assertEqual(self.request(path, dict(identity, fish='Wrong', amount=1))[0], 409)
        self.assertEqual(self.controller.catch_log(), data)
        restored = Controller(self.controller.config_path, log_sink=lambda _: None)
        self.assertEqual(restored.catch_log(), data)
        status, data = self.request('/api/catches/delete', {'session': 0, 'date': original[1]['date']})
        self.assertEqual(status, 200)
        self.assertEqual(data['sessions'], [])
        self.assertEqual(data['rates']['amount'], 0)
        self.assertIsNone(data['rates']['perHour'])
        self.assertEqual(json.loads(self.controller.catches_path.read_text()), [])

    def test_edit_remove_delete_validation_request_protection_and_write_failure(self):
        self.controller.create_catch_log(3600, 'Stormwind')
        self.controller.add_catch(0, 'Catfish', 25)
        self.controller.add_catch(0, 'Smallfish', 9007199254740991)
        before = self.controller.catch_log()
        disk = self.controller.catches_path.read_bytes()
        identity = dict(session=0, date=before['sessions'][0]['date'], index=0,
                        original=before['sessions'][0]['catches'][0])
        edit = dict(identity, fish='Catfish', amount=1, stacks=True)
        for field, invalid in {'session': [-1, True, 1, None], 'date': [None, 1],
                               'index': [-1, True, 2, None], 'original': [None, []],
                               'fish': ['', None], 'amount': [-1, 0, True, 1.5, 9007199254740992],
                               'stacks': [None, 'yes', 1]}.items():
            for value in invalid:
                with self.subTest(field=field, value=value):
                    self.assertEqual(self.request('/api/catches/edit', dict(edit, **{field: value}))[0], 400)
        self.assertEqual(self.request('/api/catches/edit', dict(edit, fish='Smallfish'))[0], 400)
        # The removed session-wide editor cannot overwrite location or duration.
        self.assertEqual(self.request('/api/catches/edit', dict(session=0, date=identity['date'],
            location='Wrong', sessionLength=1, catches=[]))[0], 400)
        for payload in (dict(identity, date=None), dict(identity, session=True), dict(identity, session=9)):
            self.assertEqual(self.request('/api/catches/delete', payload)[0], 400)
            self.assertEqual(self.request('/api/catches/remove', payload)[0], 400)
        for path, payload in [('/api/catches/edit', edit), ('/api/catches/remove', identity),
                              ('/api/catches/delete', identity)]:
            for headers in ({'X-Control-Token': ''}, {'Origin': 'https://untrusted.example'}):
                self.assertEqual(self.request(path, payload, headers)[0], 403)
            with patch.object(Path, 'replace', side_effect=OSError('Disk unavailable')):
                self.assertEqual(self.request(path, payload)[0], 500)
        self.assertEqual(self.controller.catch_log(), before)
        self.assertEqual(self.controller.catches_path.read_bytes(), disk)

    def test_catch_rates_use_all_session_time_not_average_session_rates(self):
        for duration in (3600, 7200, 3600):
            self.controller.create_catch_log(duration, 'Stormwind')
        self.controller.add_catch(0, 'Catfish', 60)
        self.controller.add_catch(1, 'Catfish', 20)
        self.controller.add_catch(1, 'Smallfish', 40)
        rates = self.request('/api/catches')[1]['rates']
        self.assertEqual(rates, {'seconds': 14400, 'amount': 120, 'perHour': 30, 'stacks': 6, 'stacksPerHour': 1.5,
                                'catches': [{'fish': 'Catfish', 'amount': 80, 'perHour': 20, 'stacks': 4, 'nonStackingAmount': 0},
                                            {'fish': 'Smallfish', 'amount': 40, 'perHour': 10, 'stacks': 2, 'nonStackingAmount': 0}]})
        self.controller.add_catch(2, 'Smallfish', 40)
        self.assertEqual(self.request('/api/catches')[1]['rates']['perHour'], 40)
        restored = Controller(self.controller.config_path, log_sink=lambda _: None)
        self.assertEqual(restored.catch_log()['rates'], self.controller.catch_log()['rates'])

    def test_location_rates_use_only_matching_logs_and_refresh_after_changes(self):
        for duration, location in [(3600, 'Stormwind'), (7200, 'Stormwind'),
                                   (3600, 'Stormwind'), (3600, 'Goldshire'), (0, 'Booty Bay')]:
            self.controller.create_catch_log(duration, location)
        self.controller.add_catch(0, 'Catfish', 19)
        self.controller.add_catch(1, 'Catfish', 2)
        self.controller.add_catch(1, 'Smallfish', 3, stacks=False)
        self.controller.add_catch(3, 'Catfish', 100)
        self.controller.add_catch(3, 'Other fish', 40)
        self.controller.add_catch(4, 'Zero-time fish', 10)
        before = self.controller.catches_path.read_bytes()
        data = self.request('/api/catches')[1]
        self.assertEqual(data['ratesByLocation']['Stormwind'], {
            'seconds': 14400, 'amount': 24, 'perHour': 6, 'stacks': 5, 'stacksPerHour': 1.25,
            'catches': [{'fish': 'Catfish', 'amount': 21, 'nonStackingAmount': 0, 'stacks': 2, 'perHour': 5.25},
                        {'fish': 'Smallfish', 'amount': 3, 'nonStackingAmount': 3, 'stacks': 3, 'perHour': .75}]})
        self.assertEqual(data['ratesByLocation']['Goldshire']['perHour'], 140)
        self.assertEqual(data['ratesByLocation']['Goldshire']['stacksPerHour'], 7)
        self.assertIsNone(data['ratesByLocation']['Booty Bay']['perHour'])
        self.assertIsNone(data['ratesByLocation']['Booty Bay']['stacksPerHour'])
        self.assertEqual(self.controller.catches_path.read_bytes(), before)
        restored = Controller(self.controller.config_path, log_sink=lambda _: None)
        self.assertEqual(restored.catch_log()['ratesByLocation'], data['ratesByLocation'])

        session = data['sessions'][0]
        changed = self.controller.edit_catch(0, session['date'], 0, session['catches'][0], 'Catfish', 39)
        self.assertEqual(changed['ratesByLocation']['Stormwind']['stacksPerHour'], 1.5)
        self.assertEqual(changed['ratesByLocation']['Goldshire'], data['ratesByLocation']['Goldshire'])
        deleted = self.controller.delete_catch_log(3, data['sessions'][3]['date'])
        self.assertNotIn('Goldshire', deleted['ratesByLocation'])
        self.assertEqual(deleted['ratesByLocation']['Stormwind'], changed['ratesByLocation']['Stormwind'])

    def test_catch_rates_with_no_catches_or_no_duration(self):
        self.controller.create_catch_log(0, 'Stormwind')
        self.controller.add_catch(0, 'Catfish', 10)
        rates = self.request('/api/catches')[1]['rates']
        self.assertIsNone(rates['perHour'])
        self.assertIsNone(rates['stacksPerHour'])
        self.assertEqual(rates['stacks'], 1)
        self.assertIsNone(rates['catches'][0]['perHour'])
        self.assertEqual(rates['amount'], 10)
        self.controller.create_catch_log(3600, 'Stormwind')
        self.assertEqual(self.request('/api/catches')[1]['rates']['perHour'], 10)
        self.controller.sessions = []
        self.controller.create_catch_log(3600, 'Stormwind')
        self.assertEqual(self.request('/api/catches')[1]['rates'], {
            'seconds': 3600, 'amount': 0, 'perHour': 0, 'catches': [], 'stacks': 0, 'stacksPerHour': 0})

    def test_stack_rates_round_each_fish_total_up_after_combining_sessions(self):
        self.controller.create_catch_log(3600, 'Stormwind')
        self.controller.create_catch_log(3600, 'Goldshire')
        self.controller.add_catch(0, 'Fish A', 21)
        self.controller.add_catch(1, 'Fish A', 2)
        self.controller.add_catch(1, 'Fish B', 50)
        rates = self.request('/api/catches')[1]['rates']
        self.assertEqual(rates['stacks'], 5)
        self.assertEqual(rates['stacksPerHour'], 2.5)
        self.assertEqual({entry['fish']: entry['stacks'] for entry in rates['catches']}, {'Fish A': 2, 'Fish B': 3})
        self.controller.add_catch(0, 'Fish C', 20)
        self.assertEqual(self.controller.catch_log()['rates']['stacks'], 6)
        self.controller.add_catch(0, 'Fish C', 1)
        self.assertEqual(self.controller.catch_log()['rates']['stacks'], 7)

    def test_bot_sessions_do_not_create_or_change_manual_catch_logs(self):
        self.controller.create_catch_log(7200, 'Stormwind')
        before = self.controller.catch_log()
        self.controller.start()
        self.controller.stop()
        self.controller.worker.join(2)
        self.assertEqual(self.controller.catch_log(), before)

    def test_stacking_flag_persists_and_mixed_lines_keep_their_counts(self):
        self.controller.create_catch_log(7200, 'Stormwind')
        for amount in (3, 2):
            status, data = self.request('/api/catches', {
                'session': 0, 'fish': 'Rare fish', 'amount': amount, 'stacks': False})
            self.assertEqual(status, 200)
        self.assertEqual(data['sessions'][0]['catches'], [{'fish': 'Rare fish', 'amount': 5, 'stacks': False}])
        self.assertEqual(data['rates']['stacks'], 5)
        self.assertEqual(data['rates']['stacksPerHour'], 2.5)
        self.assertEqual(data['rates']['catches'][0]['nonStackingAmount'], 5)
        # Different flags remain distinct lines so an add cannot reclassify previous catches.
        self.controller.add_catch(0, 'Rare fish', 23)
        self.controller.create_catch_log(3600, 'Goldshire')
        data = self.controller.add_catch(1, 'Rare fish', 1, False)
        self.assertEqual(len(data['sessions'][0]['catches']), 2)
        self.assertEqual(data['rates']['catches'][0], {
            'fish': 'Rare fish', 'amount': 29, 'nonStackingAmount': 6, 'stacks': 8, 'perHour': 29 / 3})
        restored = Controller(self.controller.config_path, log_sink=lambda _: None)
        self.assertEqual(restored.catch_log(), data)

    def test_legacy_catches_default_to_stacking_without_rewriting_on_read(self):
        self.controller.write_catches([{'date': '2026-11-06T12:00:00+00:00', 'location': 'Stormwind',
                                       'sessionLength': 3600, 'catches': [{'fish': 'Catfish', 'amount': 21}]}])
        disk = self.controller.catches_path.read_bytes()
        restored = Controller(self.controller.config_path, log_sink=lambda _: None)
        self.assertEqual(restored.catch_log()['rates']['stacks'], 2)
        self.assertEqual(self.controller.catches_path.read_bytes(), disk)
        data = restored.add_catch(0, 'Catfish', 2)
        self.assertEqual(data['sessions'][0]['catches'], [{'fish': 'Catfish', 'amount': 23, 'stacks': True}])

    def test_network_access_requires_allowed_peer_and_destination_host(self):
        self.assertEqual(self.server.server_address[0], '127.0.0.1')
        with Server(0, self.controller, ['192.168.1.0/24', '100.64.0.0/10']) as server:
            self.assertEqual(server.server_address[0], '0.0.0.0')
            handler = object.__new__(Handler)
            handler.server = server
            handler.connection = Mock()
            port = server.server_port
            for destination, peer, host, allowed in (
                ('192.168.1.233', '192.168.1.50', '192.168.1.233', True),
                ('100.104.134.32', '100.81.244.39', '100.104.134.32', True),
                ('127.0.0.1', '127.0.0.1', 'localhost', True),
                ('192.168.1.233', '8.8.8.8', '192.168.1.233', False),
                ('192.168.1.233', '192.168.2.50', '192.168.1.233', False),
                ('100.104.134.32', '100.128.0.1', '100.104.134.32', False),
                ('192.168.1.233', '192.168.1.50', 'untrusted.example', False),
                ('192.168.1.233', '192.168.1.50', '192.168.1.99', False),
            ):
                with self.subTest(destination=destination, peer=peer, host=host):
                    handler.connection.getsockname.return_value = (destination, port)
                    handler.client_address = (peer, 12345)
                    handler.headers = {'Host': f'{host}:{port}'}
                    self.assertEqual(handler.request_allowed(), allowed)

    def test_untrusted_peer_cannot_read_or_control_even_with_valid_headers(self):
        original_setup = Handler.setup
        def setup(handler):
            original_setup(handler)
            handler.client_address = ('8.8.8.8', 12345)
        with patch.object(Handler, 'setup', setup):
            self.assertEqual(self.request('/api/bootstrap')[0], 403)
            self.assertEqual(self.request('/api/preview',
                                          {'config': DEFAULTS, 'mode': 'reel', 'scenario': 25})[0], 403)


    def test_runner_failure_visible_and_can_restart(self):
        def fail(**_):
            raise RuntimeError('Test audio device unavailable')
        self.controller.runner = fail
        self.controller.start()
        self.controller.worker.join(2)
        state = self.controller.snapshot()
        self.assertEqual(state['status'], 'Error')
        self.assertEqual(state['stage']['name'], 'error')
        self.assertIn('audio device unavailable', state['error'])
        self.assertTrue(any('Test audio device' in e['message'] for e in state['logs']))
        self.controller.start()
        self.controller.worker.join(2)

    def test_stage_stream_current_state_short_transitions_and_reconnect(self):
        def read_stage(response):
            self.assertEqual(response.readline(), b'event: stage\n')
            data = response.readline()
            self.assertEqual(response.readline(), b'\n')
            return json.loads(data.removeprefix(b'data: '))
        with urlopen(self.base + '/api/stages', timeout=3) as response:
            self.assertEqual(response.headers.get_content_type(), 'text/event-stream')
            initial = read_stage(response)
            self.assertEqual(initial['name'], 'stopped')
            # Publish faster than the old one-second polling interval.
            self.controller.stage_changed('casting', .2)
            self.controller.stage_changed('listening', 59.8)
            self.controller.stage_changed('reaction', 1.234)
            changes = [read_stage(response) for _ in range(3)]
            self.assertEqual([stage['name'] for stage in changes], ['casting', 'listening', 'reaction'])
            self.assertEqual([stage['id'] for stage in changes], list(range(initial['id'] + 1, initial['id'] + 4)))
            self.assertEqual(changes[-1]['duration'], 1.234)
            refreshed = read_stage(response)
            self.assertEqual(refreshed['id'], changes[-1]['id'])
            self.assertGreater(refreshed['elapsed'], changes[-1]['elapsed'])
        with urlopen(self.base + '/api/stages', timeout=3) as response:
            resumed = read_stage(response)
            self.assertEqual(resumed['name'], 'reaction')
            self.assertGreater(resumed['elapsed'], changes[-1]['elapsed'])
        self.assertEqual(self.request('/api/state')[1]['stage']['id'], resumed['id'])

    def test_stage_snapshot_elapsed_stop_and_bounded_history(self):
        with patch('webui.time.monotonic', return_value=100):
            self.controller.stage_changed('idle', 12.5)
        with patch('webui.time.monotonic', return_value=104.25):
            stage = self.controller.snapshot()['stage']
            self.assertEqual(stage['elapsed'], 4.25)
            self.assertEqual(stage['duration'], 12.5)
        for _ in range(100):
            self.controller.stage_changed('casting', .2)
        self.assertEqual(len(self.controller.stages), 64)
        self.assertEqual(len(self.controller.stage_updates(0)), 1)
        self.assertEqual(self.controller.stage_updates(0)[0]['id'], self.controller.snapshot()['stage']['id'])
        cleanup = threading.Event()
        reported = threading.Event()
        def runner(stop_event, on_stage, **_):
            on_stage('reaction', 3)
            reported.set()
            stop_event.wait(3)
            on_stage('casting', .2)
            cleanup.wait(3)
        self.controller.runner = runner
        self.controller.start()
        self.assertTrue(reported.wait(2))
        try:
            self.assertEqual(self.controller.stop()['stage']['name'], 'stopping')
            self.controller.stage_changed('listening', 60)
            self.assertEqual(self.controller.snapshot()['stage']['name'], 'stopping')
        finally:
            cleanup.set()
            self.controller.worker.join(2)
        self.assertEqual(self.controller.snapshot()['stage']['name'], 'stopped')

    def test_audio_snapshot_age_threshold_and_stop_guard(self):
        self.assertIsNone(self.controller.snapshot()['stage']['audio'])
        with patch('webui.time.monotonic', return_value=100):
            self.controller.audio_changed(8000)
        with patch('webui.time.monotonic', return_value=100.25):
            stage = self.controller.snapshot()['stage']
            self.assertEqual(stage['threshold'], DEFAULTS['THRESHOLD'])
            self.assertEqual(stage['audio'], dict(level=8000, sampled=100, age=.25))
            self.controller.stop_event.set()
            self.controller.audio_changed(2000)
            self.assertEqual(self.controller.snapshot()['stage']['audio'], stage['audio'])

    def test_audio_stream_refreshes_while_listening_and_resets_for_a_new_session(self):
        self.controller.audio_changed(30000)
        self.controller.start()
        with urlopen(self.base + '/api/stages', timeout=3) as response:
            readings = []
            started = time.monotonic()
            while len(readings) < 3:
                self.assertEqual(response.readline(), b'event: stage\n')
                stage = json.loads(response.readline().removeprefix(b'data: '))
                self.assertEqual(response.readline(), b'\n')
                self.assertNotEqual((stage['audio'] or {}).get('level'), 30000)
                if stage['name'] == 'listening' and stage['audio'] is not None:
                    readings.append(stage['audio'])
            self.assertLess(time.monotonic() - started, 1.5)
            self.assertEqual([reading['level'] for reading in readings], [1234] * 3)
            self.assertLess(readings[0]['age'], readings[-1]['age'])

    def test_audio_heartbeat_does_not_wake_for_each_runtime_sample(self):
        self.controller.stage_changed('listening', 60)
        with patch.object(self.controller.stage_condition, 'notify_all') as notify:
            for level in range(100):
                self.controller.audio_changed(level)
            notify.assert_not_called()
        with patch.object(self.controller.stage_condition, 'wait_for') as wait:
            self.controller.stage_updates(self.controller.stages[-1]['id'])
            self.assertEqual(wait.call_args.kwargs['timeout'], .1)
            self.controller.stage_changed('paused', None)
            self.controller.stage_updates(self.controller.stages[-1]['id'])
            self.assertEqual(wait.call_args.kwargs['timeout'], 2)

    def test_stage_stream_uses_existing_access_checks(self):
        self.assertEqual(self.request('/api/stages', headers={'Host': 'untrusted.example'})[0], 403)
        original_setup = Handler.setup
        def setup(handler):
            original_setup(handler)
            handler.client_address = ('8.8.8.8', 12345)
        with patch.object(Handler, 'setup', setup):
            self.assertEqual(self.request('/api/stages')[0], 403)

    def test_slow_stage_client_does_not_block_bot_notifications(self):
        writing, release, notified = threading.Event(), threading.Event(), threading.Event()
        handler = object.__new__(Handler)
        handler.server = self.server
        handler.send_response = handler.send_header = handler.end_headers = Mock()
        handler.wfile = Mock()
        def blocked_write(_):
            writing.set()
            release.wait(3)
            raise BrokenPipeError()
        handler.wfile.write.side_effect = blocked_write
        client = threading.Thread(target=handler.stage_events, daemon=True)
        def notify():
            self.controller.stage_changed('reaction', 1.234)
            notified.set()
        bot = threading.Thread(target=notify, daemon=True)
        try:
            client.start()
            self.assertTrue(writing.wait(2))
            bot.start()
            self.assertTrue(notified.wait(1), 'A browser write blocked the bot thread')
        finally:
            release.set()
            client.join(2)
            if bot.ident is not None:
                bot.join(2)

    def test_bounded_log_and_shutdown(self):
        for i in range(205):
            self.controller.record(str(i))
        self.assertEqual(len(self.controller.snapshot()['logs']), 200)
        self.controller.start()
        self.controller.close()
        self.assertFalse(self.controller.active())
        self.assertEqual(self.request('/api/start', {})[0], 409)


if __name__ == '__main__':
    unittest.main()
