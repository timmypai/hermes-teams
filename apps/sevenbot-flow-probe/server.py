"""Small read-only delivery probe using only the Python standard library."""
import argparse
import json
import re
import signal
from http.server import BaseHTTPRequestHandler, HTTPServer


class Handler(BaseHTTPRequestHandler):
    def setup(self):
        self.request.settimeout(1.0)
        super().setup()

    def reply(self, status, document):
        payload = json.dumps(document, ensure_ascii=True).encode('utf-8')
        self.send_response(status)
        self.send_header('Content-Type', 'application/json')
        self.send_header('Content-Length', str(len(payload)))
        self.send_header('Connection', 'close')
        if status == 405:
            self.send_header('Allow', 'GET')
        self.end_headers()
        self.close_connection = True
        if self.command != 'HEAD':
            self.wfile.write(payload)

    def do_GET(self):
        version = self.server.version.copy()
        if self.path == '/healthz':
            if valid_version(version):
                self.reply(200, {'healthy': True})
            else:
                self.reply(503, {'healthy': False, 'error': 'INVALID_VERSION'})
        elif self.path == '/version':
            self.reply(200, version)
        elif self.path == '/':
            self.reply(200, {'message': 'sevenbot-' + version['response_revision']})
        else:
            self.reply(404, {'error': 'NOT_FOUND'})

    def __getattr__(self, name):
        if name.startswith('do_'):
            return self.unsupported_method
        raise AttributeError(name)

    def unsupported_method(self):
        # Never read or execute request bodies; close without draining them.
        self.reply(405, {'error': 'METHOD_NOT_ALLOWED'})

    def log_message(self, format, *args):
        pass


def valid_version(version):
    return (type(version.get('schema_version')) is int and version['schema_version'] == 1
            and isinstance(version.get('source_sha'), str)
            and re.fullmatch('[0-9a-f]{40}', version['source_sha']) is not None
            and version.get('response_revision') in ('flow-probe-v1', 'flow-probe-v2'))


def create_server(host, port, source_sha, response_revision):
    version = {'schema_version': 1, 'source_sha': source_sha,
               'response_revision': response_revision}
    if not valid_version(version) or type(port) is not int or not 0 <= port <= 65535:
        raise ValueError('INVALID_CONFIG')
    server = HTTPServer((host, port), Handler)
    server.version = version
    return server


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--host', default='127.0.0.1')
    parser.add_argument('--port', type=int, default=8080)
    parser.add_argument('--source-sha', required=True)
    parser.add_argument('--response-revision', default='flow-probe-v1')
    args = parser.parse_args(argv)
    try:
        server = create_server(args.host, args.port, args.source_sha, args.response_revision)
    except ValueError:
        parser.error('INVALID_CONFIG')
    except OSError:
        parser.error('BIND_FAILED')

    stopping = False

    def stop(signum, frame):
        # Raising here can interrupt SocketIO before its fields are initialized,
        # leaving finalization to fail. Let the bounded request finish instead.
        nonlocal stopping
        stopping = True

    previous = signal.signal(signal.SIGTERM, stop)
    try:
        print(json.dumps({'host': server.server_address[0], 'port': server.server_address[1]}), flush=True)
        server.timeout = 0.1
        while not stopping:
            server.handle_request()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
        signal.signal(signal.SIGTERM, previous)
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
