"""Fishing web UI. Use --allow-network to enable access from trusted IPv4 subnets."""
import argparse
from copy import deepcopy
from collections import deque
from datetime import datetime, timezone
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from ipaddress import IPv4Address, IPv4Network
import json
import mimetypes
import os
from pathlib import Path
import secrets
import signal
import sys
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
        self.stage_condition = threading.Condition(self.lock)
        self.stages = deque(maxlen=64)
        self._set_stage('stopped')
        self.worker = None
        self.stop_event = threading.Event()
        self.status = 'Stopped'
        self.error = None
        self.microphone = ''
        self.started = None
        self.elapsed = 0
        self.catches_path = self.config_path.with_name('catches.json')
        self.sessions = (json.loads(self.catches_path.read_text(encoding='utf-8'))
                         if self.catches_path.exists() else [])
        if not isinstance(self.sessions, list):
            raise ValueError('catches.json must contain an array of sessions.')
        self.logs = deque(maxlen=200)
        self.sequence = 0
        self.closing = False
        self.record('Web controller ready. Fishing is stopped.')

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

    def _set_stage(self, name, duration=None):
        with self.stage_condition:
            self.stages.append(dict(id=self.stages[-1]['id'] + 1 if self.stages else 0,
                                    name=name, duration=duration, started=time.monotonic()))
            self.stage_condition.notify_all()

    def stage_changed(self, name, duration):
        with self.lock:
            if not self.stop_event.is_set() and not self.closing:
                self._set_stage(name, duration)

    def stage_snapshot(self, stage):
        return dict(id=stage['id'], name=stage['name'], duration=stage['duration'],
                    elapsed=max(0, time.monotonic() - stage['started']))

    def stage_updates(self, after):
        with self.stage_condition:
            self.stage_condition.wait_for(lambda: self.closing or self.stages[-1]['id'] > after, timeout=2)
            # Slow or reconnected pages resume at the current stage, not an old cycle.
            if after < self.stages[0]['id'] - 1:
                return [self.stage_snapshot(self.stages[-1])]
            stages = [stage for stage in self.stages if stage['id'] > after]
            return [self.stage_snapshot(stage) for stage in stages or [self.stages[-1]]]

    def snapshot(self):
        with self.lock:
            active = self.active()
            return dict(status=self.status, active=active, error=self.error, microphone=self.microphone,
                        elapsed=time.monotonic() - self.started if active else self.elapsed,
                        config=self.config.copy(), logs=list(self.logs),
                        stage=self.stage_snapshot(self.stages[-1]))

    def save(self, values):
        values = validate(values)
        with self.lock:
            if self.closing or self.active():
                raise Conflict('Stop fishing before saving settings.')
            self.config = save_config(values, self.config_path)
            self.record('Configuration saved. It will be used for the next session.')
            return self.config.copy()

    def catch_log(self):
        with self.lock:
            sessions = deepcopy(self.sessions)
            names = sorted({catch['fish'] for session in sessions for catch in session['catches']},
                           key=str.casefold)
            locations = {}
            for session in sessions:
                locations.setdefault(session['location'], []).append(session)
            return dict(sessions=sessions, fishNames=names, rates=self.catch_rates(sessions),
                        ratesByLocation={location: self.catch_rates(logs) for location, logs in locations.items()})

    @staticmethod
    def catch_rates(sessions):
        names = {catch['fish'] for session in sessions for catch in session['catches']}
        seconds = sum(session['sessionLength'] for session in sessions)
        amounts = dict.fromkeys(names, 0)
        non_stacking = dict.fromkeys(names, 0)
        for session in sessions:
            for catch in session['catches']:
                amounts[catch['fish']] += catch['amount']
                if not catch.get('stacks', True):
                    non_stacking[catch['fish']] += catch['amount']
        total = sum(amounts.values())
        rates = dict(seconds=seconds, amount=total, perHour=total * 3600 / seconds if seconds else None,
                     catches=[dict(fish=fish, amount=amount, nonStackingAmount=non_stacking[fish],
                                   stacks=(amount - non_stacking[fish] + 19) // 20 + non_stacking[fish],
                                   perHour=amount * 3600 / seconds if seconds else None)
                              for fish, amount in sorted(amounts.items(), key=lambda item: (-item[1], item[0].casefold()))])
        rates['stacks'] = sum(catch['stacks'] for catch in rates['catches'])
        rates['stacksPerHour'] = rates['stacks'] * 3600 / seconds if seconds else None
        return rates

    def create_catch_log(self, duration, location):
        if type(duration) is not int or not 0 <= duration <= 9007199254740991:
            raise ValueError('Session length must be a nonnegative whole number of seconds within the safe integer range.')
        if not isinstance(location, str) or not 1 <= len(location.strip()) <= 120:
            raise ValueError('Enter a location between 1 and 120 characters.')

        with self.lock:
            if self.closing:
                raise Conflict('The controller is shutting down.')
            session = dict(date=datetime.now(timezone.utc).isoformat(), location=location.strip(),
                           sessionLength=duration, catches=[])
            self.write_catches(self.sessions + [session])
            return self.catch_log()

    def write_catches(self, sessions):
        self.catches_path.parent.mkdir(parents=True, exist_ok=True)
        temporary = self.catches_path.with_suffix('.json.tmp')
        temporary.write_text(json.dumps(sessions, indent=2, ensure_ascii=False) + '\n', encoding='utf-8')
        temporary.replace(self.catches_path)
        self.sessions = sessions

    @staticmethod
    def validate_catch(fish, amount, stacks):
        if not isinstance(fish, str) or not 1 <= len(fish.strip()) <= 120:
            raise ValueError('Enter a fish name between 1 and 120 characters.')
        if type(amount) is not int or not 1 <= amount <= 9007199254740991:
            raise ValueError('Amount must be a positive whole number within the safe integer range.')
        if type(stacks) is not bool:
            raise ValueError('Stacks must be true or false.')

    def check_catch_session(self, session, date=None):
        if type(session) is not int or not 0 <= session < len(self.sessions):
            raise ValueError('Choose an existing fishing session.')
        if date is not None and date != self.sessions[session]['date']:
            raise Conflict('The saved session list changed. Reload it and try again.')
        if self.closing:
            raise Conflict('The controller is shutting down.')

    def add_catch(self, session, fish, amount, stacks=True, date=None):
        self.validate_catch(fish, amount, stacks)
        fish = fish.strip()
        with self.lock:
            self.check_catch_session(session, date)
            sessions = self.catch_log()['sessions']
            names = {catch['fish'].casefold(): catch['fish']
                     for entry in sessions for catch in entry['catches']}
            fish = names.get(fish.casefold(), fish)
            catches = sessions[session]['catches']
            for catch in catches:
                if catch['fish'] == fish and catch.get('stacks', True) == stacks:
                    if catch['amount'] + amount > 9007199254740991:
                        raise ValueError('The total amount exceeds the safe integer range.')
                    catch['amount'] += amount
                    catch['stacks'] = stacks
                    break
            else:
                catches.append(dict(fish=fish, amount=amount, stacks=stacks))
            self.write_catches(sessions)
            return self.catch_log()

    def check_catch_row(self, session, date, index, original):
        if not isinstance(date, str):
            raise ValueError('Provide the original session date.')
        self.check_catch_session(session, date)
        catches = self.sessions[session]['catches']
        if type(index) is not int or not 0 <= index < len(catches):
            raise ValueError('Choose an existing fish row.')
        if not isinstance(original, dict):
            raise ValueError('Provide the original fish row.')
        if original != catches[index]:
            raise Conflict('This fish row changed. Cancel your edit and reload the saved log.')

    def edit_catch(self, session, date, index, original, fish, amount, stacks=True):
        self.validate_catch(fish, amount, stacks)
        with self.lock:
            self.check_catch_row(session, date, index, original)
            names = {catch['fish'].casefold(): catch['fish']
                     for entry in self.sessions for catch in entry['catches']}
            fish = names.get(fish.strip().casefold(), fish.strip())
            sessions = deepcopy(self.sessions)
            catches = sessions[session]['catches']
            for other_index, catch in enumerate(catches):
                if other_index != index and catch['fish'] == fish and catch.get('stacks', True) == stacks:
                    if catch['amount'] + amount > 9007199254740991:
                        raise ValueError('The total amount exceeds the safe integer range.')
                    catch['amount'] += amount
                    catch['stacks'] = stacks
                    del catches[index]
                    break
            else:
                catches[index] = dict(fish=fish, amount=amount, stacks=stacks)
            self.write_catches(sessions)
            return self.catch_log()

    def remove_catch(self, session, date, index, original):
        with self.lock:
            self.check_catch_row(session, date, index, original)
            sessions = deepcopy(self.sessions)
            del sessions[session]['catches'][index]
            self.write_catches(sessions)
            return self.catch_log()

    def delete_catch_log(self, session, date):
        if not isinstance(date, str):
            raise ValueError('Provide the original session date.')
        with self.lock:
            self.check_catch_session(session, date)
            sessions = deepcopy(self.sessions)
            del sessions[session]
            self.write_catches(sessions)
            return self.catch_log()

    def start(self):
        with self.lock:
            if self.closing or self.active():
                raise Conflict('A session is already active or the server is shutting down.')
            config = self.config.copy()
            self.stop_event = threading.Event()
            self.status, self.error = 'Starting', None
            self.microphone = ''
            self._set_stage('starting')
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
            runner(config=config, stop_event=self.stop_event, log=self.record, on_ready=ready,
                   on_stage=self.stage_changed)
            with self.lock:
                self.status = 'Stopped'
                self._set_stage('stopped')
        except Exception as exc:
            self.record(traceback.format_exc())
            with self.lock:
                self.error = str(exc)
                self.status = 'Error'
                self._set_stage('error')
        finally:
            with self.lock:
                self.elapsed = time.monotonic() - self.started

    def stop(self):
        with self.lock:
            if self.active() and self.status in ('Starting', 'Running', 'Stopping'):
                self.stop_event.set()
                self.status = 'Stopping'
                self._set_stage('stopping')
                self.record('Stop requested. Waiting for hardware cleanup.')
        return self.snapshot()

    def close(self):
        with self.lock:
            self.closing = True
            self.stage_condition.notify_all()
            self.stop()
            worker = self.worker
        if worker is not None:
            worker.join(timeout=10)
            if worker.is_alive():
                self.record('Still waiting for a device call to return. Process remains alive for cleanup.')

    def check_microphone(self):
        audio = stream = None
        try:
            import pyaudio
            audio = pyaudio.PyAudio()
            device = audio.get_default_input_device_info()
            stream = audio.open(format=pyaudio.paInt16, channels=self.config['CHANNELS'],
                                rate=self.config['RATE'], input=True, frames_per_buffer=1024)
            data = stream.read(1024, exception_on_overflow=False)
            if len(data) != 1024 * self.config['CHANNELS'] * 2:
                raise OSError('The microphone returned incomplete audio.')
        finally:
            try:
                if stream is not None:
                    stream.close()
            finally:
                if audio is not None:
                    audio.terminate()
        self.microphone = (f"Microphone ready: {device['name']} at {self.config['RATE']} Hz, "
                           f"{self.config['CHANNELS']} channel(s). Fishing is stopped.")
        self.record(self.microphone)


class Server(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = True

    def __init__(self, port, controller, allowed_networks=()):
        self.controller = controller
        self.restart_requested = False
        self.token = secrets.token_urlsafe(32)
        self.allowed_networks = (IPv4Network('127.0.0.0/8'),
                                 *(IPv4Network(network) for network in allowed_networks))
        host = '0.0.0.0' if allowed_networks else '127.0.0.1'
        super().__init__((host, port), Handler)

    def request_restart(self):
        controller = self.controller
        with controller.lock:
            if controller.closing or controller.active():
                raise Conflict('Stop fishing and wait for cleanup before reconnecting the microphone.')
            controller.closing = True
            controller.status = 'Restarting'
            controller.error = None
            controller.stage_condition.notify_all()
            self.restart_requested = True
            controller.record('Restarting the controller to reconnect the microphone. Fishing stays stopped.')
            return controller.snapshot()


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
        elif path == '/api/stages':
            self.stage_events()
        elif path == '/api/catches':
            self.json(self.server.controller.catch_log())
        else:
            relative = 'index.html' if path == '/' else path.lstrip('/')
            file = (WEB_ROOT / relative).resolve()
            if not file.is_relative_to(WEB_ROOT) or not file.is_file():
                self.json({'error': 'Not found'}, 404)
                return
            content_type = {'.js': 'text/javascript', '.css': 'text/css', '.html': 'text/html'}.get(file.suffix)
            self.send_bytes(200, file.read_bytes(), content_type or mimetypes.guess_type(file.name)[0] or 'application/octet-stream')

    def stage_events(self):
        self.send_response(200)
        self.send_header('Content-Type', 'text/event-stream')
        self.send_header('Cache-Control', 'no-store')
        self.send_header('X-Content-Type-Options', 'nosniff')
        self.send_header('Connection', 'close')
        self.end_headers()
        controller = self.server.controller
        with controller.lock:
            updates = [controller.stage_snapshot(controller.stages[-1])]
        try:
            while not controller.closing:
                for stage in updates:
                    self.wfile.write(('event: stage\ndata: ' + json.dumps(stage, allow_nan=False) + '\n\n').encode())
                    self.wfile.flush()
                updates = controller.stage_updates(updates[-1]['id'])
        except OSError:
            pass

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
            elif path == '/api/reconnect-microphone':
                state = self.server.request_restart()
                try:
                    self.json(state, HTTPStatus.ACCEPTED)
                finally:
                    threading.Thread(target=self.server.shutdown, daemon=True).start()
            elif path == '/api/catches':
                self.json(controller.add_catch(payload.get('session'), payload.get('fish'), payload.get('amount'),
                                               payload.get('stacks', True), payload.get('date')))
            elif path == '/api/catches/new':
                self.json(controller.create_catch_log(payload.get('sessionLength'), payload.get('location')))
            elif path == '/api/catches/edit':
                self.json(controller.edit_catch(payload.get('session'), payload.get('date'), payload.get('index'),
                                               payload.get('original'), payload.get('fish'), payload.get('amount'),
                                               payload.get('stacks', True)))
            elif path == '/api/catches/remove':
                self.json(controller.remove_catch(payload.get('session'), payload.get('date'),
                                                 payload.get('index'), payload.get('original')))
            elif path == '/api/catches/delete':
                self.json(controller.delete_catch_log(payload.get('session'), payload.get('date')))
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
    parser.add_argument('--check-microphone', action='store_true',
                        help='Check microphone capture at startup without starting fishing.')
    args = parser.parse_args()
    controller = Controller()
    if args.check_microphone:
        try:
            controller.check_microphone()
        except Exception as exc:
            controller.status = 'Error'
            controller.error = f'Microphone check failed: {exc}'
            controller._set_stage('error')
            controller.record(controller.error)
    server = Server(args.port, controller, args.allow_network)
    def shutdown(*_):
        server.restart_requested = False
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
    if server.restart_requested:
        arguments = [sys.executable, *sys.argv]
        if '--check-microphone' not in arguments:
            arguments.append('--check-microphone')
        os.execv(sys.executable, arguments)


if __name__ == '__main__':
    main()
