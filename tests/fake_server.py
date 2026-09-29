"""A tiny in-process Jellyfin stand-in for /System/Configuration/encoding."""

import copy
import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

KEY = "0" * 31 + "1"


class FakeJellyfin:
    def __init__(self, encoding: dict, drop_keys=()):
        self.encoding = copy.deepcopy(encoding)
        self.drop_keys = set(drop_keys)  # simulate a server that ignores some fields
        self.posts = []
        self.auth_headers = []
        outer = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *a):
                pass

            def _authed(self):
                auth = self.headers.get("Authorization", "")
                outer.auth_headers.append(auth)
                return f'Token="{KEY}"' in auth

            def _send(self, code, body=None):
                data = b"" if body is None else json.dumps(body).encode()
                self.send_response(code)
                if data:
                    self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)

            def do_GET(self):
                if self.path == "/System/Info/Public":
                    return self._send(200, {"Version": "10.10.7"})
                if self.path != "/System/Configuration/encoding":
                    return self._send(404)
                if not self._authed():
                    return self._send(401)
                self._send(200, outer.encoding)

            def do_POST(self):
                if self.path != "/System/Configuration/encoding":
                    return self._send(404)
                if not self._authed():
                    return self._send(401)
                body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
                outer.posts.append(body)
                for k, v in body.items():
                    if k not in outer.drop_keys:
                        outer.encoding[k] = v
                self._send(204)

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.url = f"http://127.0.0.1:{self.server.server_address[1]}"
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)

    def __enter__(self):
        self.thread.start()
        return self

    def __exit__(self, *exc):
        self.server.shutdown()
        self.server.server_close()
