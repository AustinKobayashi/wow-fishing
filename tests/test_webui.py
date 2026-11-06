import json
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
from webui import Controller, Handler, Server


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
        def runner(config, stop_event, log, on_ready):
            self.calls.append(config)
            on_ready()
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
            'sessions': [], 'fishNames': [], 'rates': {
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
        self.assertIn('audio device unavailable', state['error'])
        self.assertTrue(any('Test audio device' in e['message'] for e in state['logs']))
        self.controller.start()
        self.controller.worker.join(2)

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
