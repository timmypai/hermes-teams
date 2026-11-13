"""Real loopback contract checks; no external services or mock HTTP."""
import importlib.util
import json
from pathlib import Path
import http.client
import threading
import unittest
import socket
import subprocess
import sys
import tempfile
import time

ROOT = Path(__file__).resolve().parents[1]
ENTRY = ROOT / 'apps/sevenbot-flow-probe/server.py'
SHA = '7919e4c595b37b95e86d22e2d025b62655e8dd58'  # accepted test fixture, not deployment proof


class FlowProbe(unittest.TestCase):
    def load_app(self):
        self.assertTrue(ENTRY.is_file(), 'application entrypoint must exist')
        spec = importlib.util.spec_from_file_location('flow_probe', ENTRY)
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        return module

    def start(self, revision='flow-probe-v1'):
        app = self.load_app()
        server = app.create_server('127.0.0.1', 0, SHA, revision)
        thread = threading.Thread(target=server.serve_forever, kwargs={'poll_interval': 0.01})
        thread.start()
        def cleanup():
            server.shutdown()
            server.server_close()
            thread.join(3)
            self.assertFalse(thread.is_alive())
        self.addCleanup(cleanup)
        return server

    def request(self, server, path, method='GET', body=None):
        connection = http.client.HTTPConnection(*server.server_address, timeout=3)
        try:
            connection.request(method, path, body=body)
            response = connection.getresponse()
            payload = response.read()
            self.assertEqual(response.getheader('Content-Type'), 'application/json')
            if method != 'HEAD':
                self.assertEqual(int(response.getheader('Content-Length')), len(payload))
            return response.status, json.loads(payload) if payload else None
        finally:
            connection.close()

    def test_real_version_identity(self):
        server = self.start()
        self.assertEqual(self.request(server, '/version'), (200, {
            'schema_version': 1, 'source_sha': SHA, 'response_revision': 'flow-probe-v1'}))

    def test_revision_messages_and_health(self):
        for revision, message in [('flow-probe-v1', 'sevenbot-flow-probe-v1'),
                                  ('flow-probe-v2', 'sevenbot-flow-probe-v2')]:
            with self.subTest(revision=revision):
                server = self.start(revision)
                self.assertEqual(self.request(server, '/'), (200, {'message': message}))
                self.assertEqual(self.request(server, '/healthz'), (200, {'healthy': True}))
                self.assertEqual(self.request(server, '/version')[1]['response_revision'], revision)

    def test_health_rechecks_running_state(self):
        server = self.start()
        for key, value in [('source_sha', 'secret-invalid'), ('response_revision', 'invalid'),
                           ('schema_version', 2)]:
            with self.subTest(key=key):
                old = server.version[key]
                server.version[key] = value  # in-process fault, never an HTTP mutation endpoint
                self.assertEqual(self.request(server, '/healthz'),
                                 (503, {'healthy': False, 'error': 'INVALID_VERSION'}))
                server.version[key] = old
                self.assertEqual(self.request(server, '/healthz'), (200, {'healthy': True}))

    def test_unknown_routes_are_not_files_or_reflections(self):
        server = self.start()
        for path in ['/missing', '/server.py', '/../../etc/passwd', '/?secret=%22', '/config']:
            with self.subTest(path=path):
                self.assertEqual(self.request(server, path), (404, {'error': 'NOT_FOUND'}))

    def test_unsupported_methods_do_not_execute_body(self):
        server = self.start()
        for method in ['POST', 'PUT', 'PATCH', 'DELETE', 'OPTIONS', 'TRACE', 'CONNECT', 'CUSTOM']:
            with self.subTest(method=method):
                self.assertEqual(self.request(server, '/', method, '$(touch forbidden); secret'),
                                 (405, {'error': 'METHOD_NOT_ALLOWED'}))
        self.assertEqual(self.request(server, '/', 'HEAD'), (405, None))

    def test_invalid_config_before_bind(self):
        app = self.load_app()
        with socket.socket() as occupied:
            occupied.bind(('127.0.0.1', 0))
            occupied.listen()
            for sha, revision, port in [(s, 'flow-probe-v1', occupied.getsockname()[1])
                                       for s in ['', 'a'*39, 'a'*41, 'A'*40, 'g'*40, SHA+'\n']]:
                with self.subTest(sha=sha), self.assertRaises(ValueError):
                    app.create_server('127.0.0.1', port, sha, revision)
            for revision, port in [('v1', 0), ('flow-probe-v3', 0), ('flow-probe-v1', -1),
                                   ('flow-probe-v1', 65536)]:
                with self.subTest(revision=revision, port=port), self.assertRaises(ValueError):
                    app.create_server('127.0.0.1', port, SHA, revision)

    def test_cli_invalid_config_nonzero_without_listener(self):
        cases = [[], ['--source-sha', 'latest'], ['--source-sha', SHA, '--port', '-1'],
                 ['--source-sha', SHA, '--port', '65536'],
                 ['--source-sha', SHA, '--port', 'not-a-port'],
                 ['--source-sha', SHA, '--response-revision', 'bad']]
        for args in cases:
            with self.subTest(args=args):
                result = subprocess.run([sys.executable, str(ENTRY), *args], cwd=ROOT,
                                        capture_output=True, text=True, timeout=3)
                self.assertEqual(result.returncode, 2, result.stderr)
                self.assertEqual(result.stdout, '')
                self.assertIn('error:', result.stderr)

    def test_invalid_entrypoint_never_attempts_bind(self):
        # Audit real socket.bind calls while running the actual entrypoint in a child.
        # A bind attempt raises independently of whether a port would be available.
        script = (
            "import runpy, sys\n"
            "def audit(event, args):\n"
            "    if event == 'socket.bind': raise RuntimeError('BIND_ATTEMPTED')\n"
            "sys.addaudithook(audit)\n"
            "sys.argv = [sys.argv[1], '--port', '0', '--source-sha', 'invalid']\n"
            "runpy.run_path(sys.argv[0], run_name='__main__')\n"
        )
        result = subprocess.run([sys.executable, '-c', script, str(ENTRY)], cwd=ROOT,
                                capture_output=True, text=True, timeout=3)
        self.assertEqual(result.returncode, 2)
        self.assertIn('INVALID_CONFIG', result.stderr)
        self.assertNotIn('BIND_ATTEMPTED', result.stderr)

    def test_oversized_request_is_rejected(self):
        server = self.start()
        with socket.create_connection(server.server_address, timeout=3) as client:
            client.sendall(b'GET /' + b'x' * 65536 + b' HTTP/1.1\r\n\r\n')
            self.assertIn(b'414', client.recv(1024).split(b'\r\n')[0])
        self.assertEqual(self.request(server, '/healthz'), (200, {'healthy': True}))

    def test_foreground_cli_http_and_sigterm(self):
        cache = ROOT / '.cache'
        cache.mkdir(exist_ok=True)
        with tempfile.TemporaryDirectory(prefix='probe-', dir=cache) as directory:
            with open(Path(directory) / 'stdout', 'w+') as output, \
                 open(Path(directory) / 'stderr', 'w+') as errors:
                proc = subprocess.Popen([sys.executable, str(ENTRY), '--port', '0',
                                         '--source-sha', SHA], cwd=ROOT,
                                        stdout=output, stderr=errors)
                try:
                    deadline = time.monotonic() + 3
                    line = ''
                    while time.monotonic() < deadline and proc.poll() is None:
                        output.seek(0)
                        line = output.readline()
                        if line:
                            break
                        time.sleep(0.01)
                    self.assertTrue(line, 'foreground process must announce its actual bound port')
                    address = json.loads(line)
                    self.assertEqual(address['host'], '127.0.0.1')
                    conn = http.client.HTTPConnection(address['host'], address['port'], timeout=2)
                    try:
                        conn.request('GET', '/version')
                        response = conn.getresponse()
                        self.assertEqual(response.status, 200)
                        self.assertEqual(json.loads(response.read())['source_sha'], SHA)
                    finally:
                        conn.close()
                    # SIGTERM must also terminate promptly with an incomplete request in flight.
                    with socket.create_connection((address['host'], address['port']), timeout=2) as idle:
                        idle.sendall(b'GET / HTTP/1.1\r\n')
                        proc.terminate()
                        self.assertEqual(proc.wait(timeout=3), 0)
                    with socket.socket() as closed:
                        closed.settimeout(1)
                        self.assertNotEqual(closed.connect_ex((address['host'], address['port'])), 0)
                    errors.seek(0)
                    self.assertEqual(errors.read(), '')
                finally:
                    if proc.poll() is None:
                        proc.kill()
                    proc.wait(timeout=3)

    def test_incomplete_request_has_bounded_read(self):
        server = self.start()
        with socket.create_connection(server.server_address, timeout=3) as client:
            client.settimeout(3)
            client.sendall(b'GET / HTTP/1.1\r\n')
            self.assertEqual(client.recv(1), b'')
        self.assertEqual(self.request(server, '/healthz'), (200, {'healthy': True}))
