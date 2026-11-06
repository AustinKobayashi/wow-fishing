import json
from pathlib import Path
import tempfile
import threading
import time
import unittest
from unittest.mock import Mock, patch
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
