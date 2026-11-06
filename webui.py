"""Fishing web UI. Use --allow-network to enable access from trusted IPv4 subnets."""
import argparse
from collections import deque
from datetime import datetime
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from ipaddress import IPv4Address, IPv4Network
import json
import mimetypes
from pathlib import Path
import secrets
import signal
import threading
import time
import traceback
from urllib.parse import urlsplit

from configuration import CONFIG_PATH, DEFAULTS, SCHEMA, load_config, save_config, validate
import logger
from normal_distribution import preview

WEB_ROOT = Path(__file__).resolve().parent / 'web'


class Conflict(Exception):
    pass


class Controller:
    def __init__(self, config_path=CONFIG_PATH, runner=None, log_sink=logger.log):
        self.config_path = Path(config_path)
        self.config = load_config(self.config_path)
        self.runner = runner
        self.log_sink = log_sink
        self.lock = threading.RLock()
        self.worker = None
        self.stop_event = threading.Event()
        self.status = 'Stopped'
        self.error = None
        self.started = None
        self.elapsed = 0
        self.logs = deque(maxlen=200)
        self.sequence = 0
        self.closing = False
        self.record('Web controller ready. Hardware connects only when you start fishing.')

    def record(self, message):
        message = str(message)
        with self.lock:
            self.sequence += 1
            self.logs.append(dict(id=self.sequence, time=datetime.now().strftime('%H:%M:%S'),
                                  message=message[:4000]))
        try:
            self.log_sink(message)
        except Exception as exc:
            with self.lock:
                self.sequence += 1
                self.logs.append(dict(id=self.sequence, time=datetime.now().strftime('%H:%M:%S'),
                                      message=f'Could not write disk log: {exc}'))

    def active(self):
        return self.worker is not None and self.worker.is_alive()

    def snapshot(self):
        with self.lock:
            active = self.active()
            return dict(status=self.status, active=active, error=self.error,
                        elapsed=time.monotonic() - self.started if active else self.elapsed,
                        config=self.config.copy(), logs=list(self.logs))

    def save(self, values):
        values = validate(values)
        with self.lock:
            if self.closing or self.active():
                raise Conflict('Stop fishing before saving settings.')
            self.config = save_config(values, self.config_path)
            self.record('Configuration saved. It will be used for the next session.')
            return self.config.copy()

    def start(self):
        with self.lock:
            if self.closing or self.active():
                raise Conflict('A session is already active or the server is shutting down.')
            config = self.config.copy()
            self.stop_event = threading.Event()
            self.status, self.error = 'Starting', None
            self.started = time.monotonic()
            self.elapsed = 0
            self.worker = threading.Thread(target=self._run, args=(config,), name='fishing-bot', daemon=False)
            self.worker.start()
        return self.snapshot()

    def _run(self, config):
        def ready():
            with self.lock:
                if not self.stop_event.is_set():
                    self.status = 'Running'
        try:
            self.record('Starting fishing with saved settings.')
            runner = self.runner
            if runner is None:
                from main import main
                runner = main
            runner(config=config, stop_event=self.stop_event, log=self.record, on_ready=ready)
            with self.lock:
                self.status = 'Stopped'
        except Exception as exc:
            self.record(traceback.format_exc())
            with self.lock:
                self.error = str(exc)
                self.status = 'Error'
        finally:
            with self.lock:
                self.elapsed = time.monotonic() - self.started

    def stop(self):
        with self.lock:
            if self.active() and self.status in ('Starting', 'Running', 'Stopping'):
                self.stop_event.set()
                self.status = 'Stopping'
                self.record('Stop requested. Waiting for hardware cleanup.')
        return self.snapshot()

    def close(self):
        with self.lock:
            self.closing = True
            self.stop()
            worker = self.worker
        if worker is not None:
            worker.join(timeout=10)
            if worker.is_alive():
                self.record('Still waiting for a device call to return. Process remains alive for cleanup.')


class Server(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = True

    def __init__(self, port, controller, allowed_networks=()):
        self.controller = controller
        self.token = secrets.token_urlsafe(32)
        self.allowed_networks = (IPv4Network('127.0.0.0/8'),
                                 *(IPv4Network(network) for network in allowed_networks))
        host = '0.0.0.0' if allowed_networks else '127.0.0.1'
        super().__init__((host, port), Handler)


class Handler(BaseHTTPRequestHandler):
    def setup(self):
        super().setup()
        self.connection.settimeout(5)

    def log_message(self, *_):
        pass

    def send_bytes(self, status, body, content_type='application/json'):
        self.send_response(status)
        self.send_header('Content-Type', content_type)
        self.send_header('Content-Length', str(len(body)))
        self.send_header('Cache-Control', 'no-store')
        self.send_header('X-Content-Type-Options', 'nosniff')
        self.send_header('Referrer-Policy', 'no-referrer')
        self.send_header('Content-Security-Policy', "default-src 'self'\x3b script-src 'self'\x3b style-src 'self' 'unsafe-inline'\x3b img-src 'self' data:\x3b connect-src 'self'\x3b frame-ancestors 'none'\x3b base-uri 'none'")
        self.end_headers()
        self.wfile.write(body)

    def json(self, data, status=HTTPStatus.OK):
        self.send_bytes(status, json.dumps(data, allow_nan=False).encode('utf-8'))

    def request_allowed(self):
        address = IPv4Address(self.client_address[0])
        if not any(address in network for network in self.server.allowed_networks):
            return False
        # Match the actual destination IP to prevent DNS rebinding via foreign hosts.
        port = self.server.server_port
        return self.headers.get('Host') in (f'127.0.0.1:{port}', f'localhost:{port}',
                                           f'{self.connection.getsockname()[0]}:{port}')

    def do_GET(self):
        if not self.request_allowed():
            self.json({'error': 'Access is limited to the configured local networks.'}, 403)
            return
        path = urlsplit(self.path).path
        if path == '/api/bootstrap':
            self.json(dict(schema=SCHEMA, defaults=DEFAULTS, token=self.server.token,
                           state=self.server.controller.snapshot()))
        elif path == '/api/state':
            self.json(self.server.controller.snapshot())
        else:
            relative = 'index.html' if path == '/' else path.lstrip('/')
            file = (WEB_ROOT / relative).resolve()
            if not file.is_relative_to(WEB_ROOT) or not file.is_file():
                self.json({'error': 'Not found'}, 404)
                return
            content_type = {'.js': 'text/javascript', '.css': 'text/css', '.html': 'text/html'}.get(file.suffix)
            self.send_bytes(200, file.read_bytes(), content_type or mimetypes.guess_type(file.name)[0] or 'application/octet-stream')

    def do_POST(self):
        origin = self.headers.get('Origin')
        if (not self.request_allowed() or
                (origin is not None and origin != 'http://' + self.headers.get('Host', '')) or
                not secrets.compare_digest(self.headers.get('X-Control-Token', ''), self.server.token)):
            self.json({'error': 'Reload this page from the fishing controller before making changes.'}, 403)
            return
        try:
            length = int(self.headers.get('Content-Length', '0'))
            if not 0 < length <= 32768:
                raise ValueError('JSON request must be between 1 and 32768 bytes.')
            if self.headers.get_content_type() != 'application/json':
                raise ValueError('Use application/json.')
            payload = json.loads(self.rfile.read(length))
            if not isinstance(payload, dict):
                raise ValueError('Expected a JSON object.')
            path = urlsplit(self.path).path
            controller = self.server.controller
            if path == '/api/config':
                self.json(dict(config=controller.save(payload.get('config'))))
            elif path == '/api/preview':
                self.json(preview(validate(payload.get('config')), payload.get('mode'), payload.get('scenario')))
            elif path == '/api/start':
                self.json(controller.start())
            elif path == '/api/stop':
                self.json(controller.stop())
            else:
                self.json({'error': 'Not found'}, 404)
        except Conflict as exc:
            self.json({'error': str(exc)}, 409)
        except (ValueError, TypeError, KeyError) as exc:
            self.json({'error': str(exc)}, 400)
        except Exception:
            self.server.controller.record(traceback.format_exc())
            self.json({'error': 'Server error. See the fishing journal.'}, 500)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--port', type=int, default=8766)
    parser.add_argument('--allow-network', action='append', type=IPv4Network, default=[],
                        help='Allow an IPv4 subnet and listen on the network. May be repeated.')
    args = parser.parse_args()
    controller = Controller()
    server = Server(args.port, controller, args.allow_network)
    def shutdown(*_):
        controller.stop_event.set()
        # shutdown must run outside the serve_forever thread.
        threading.Thread(target=server.shutdown, daemon=True).start()
    signal.signal(signal.SIGINT, shutdown)
    signal.signal(signal.SIGTERM, shutdown)
    print(f"The Angler's Rest listening on {server.server_address[0]}:{server.server_port}", flush=True)
    try:
        server.serve_forever(poll_interval=.2)
    finally:
        controller.close()
        server.server_close()


if __name__ == '__main__':
    main()
