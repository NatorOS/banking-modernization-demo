"""Local dashboard server.

Local mode (SQLite):   python3 -m modern.server
AWS mode (SigV4 proxy): python3 -m modern.server --remote https://<api-id>.execute-api.us-east-1.amazonaws.com --profile natoros

Binds to 127.0.0.1 only. In AWS mode the browser talks to this local process, which signs each
/api request with the operator's own AWS credentials; nothing is exposed without IAM auth.
"""
import argparse
import json
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlsplit

from .api import MAX_BODY_BYTES
from .service import validate_namespace

DASHBOARD = Path(__file__).with_name("dashboard.html")


def make_handler(backend, label, namespace="demo-local"):
    validate_namespace(namespace)
    class Handler(BaseHTTPRequestHandler):
        server_version = "banking-demo"

        def _send(self, status, headers, body):
            self.send_response(status)
            for k, v in headers.items():
                self.send_header(k, v)
            self.send_header("content-length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def _dispatch(self, method):
            url = urlsplit(self.path)
            if method == "GET" and url.path in ("/", "/index.html"):
                html = DASHBOARD.read_text().replace("__BACKEND_LABEL__", label).replace(
                    'value="demo-local"', f'value="{namespace}"').encode()
                return self._send(200, {"content-type": "text/html; charset=utf-8", "cache-control": "no-store"}, html)
            if not url.path.startswith("/api/"):
                return self._send(404, {"content-type": "text/plain"}, b"not found")
            length = int(self.headers.get("content-length") or 0)
            if length > MAX_BODY_BYTES:
                return self._send(413, {"content-type": "application/json"}, b'{"error":{"code":"body_too_large"}}')
            body = self.rfile.read(length) if length else b""
            forwarded = {k: v for k, v in self.headers.items()
                         if k.lower() in ("content-type", "idempotency-key", "x-provider-timestamp", "x-provider-signature")}
            status, payload = backend(method, url.path, body, forwarded, url.query)
            self._send(status, {"content-type": "application/json", "cache-control": "no-store"}, payload)

        def do_GET(self):
            self._dispatch("GET")

        def do_POST(self):
            self._dispatch("POST")

        def log_message(self, fmt, *args):
            print(json.dumps({"method": self.command, "path": urlsplit(self.path).path, "status": args[1] if len(args) > 1 else None}))

    return Handler


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--port", type=int, default=8000)
    parser.add_argument("--db", default="data/modern.sqlite")
    parser.add_argument("--remote")
    parser.add_argument("--profile")
    parser.add_argument("--region", default="us-east-1")
    parser.add_argument("--namespace", help="initial dashboard namespace (AWS default: demo-aws)")
    args = parser.parse_args(argv)
    if args.remote:
        from .aws_client import SignedApiClient
        client = SignedApiClient(args.remote, args.profile, args.region)
        label = f"AWS (SigV4 proxy to {urlsplit(args.remote).netloc})"
    else:
        from .aws_client import LocalClient
        from .config import build_app
        client = LocalClient(build_app(sqlite_path=args.db))
        label = f"local SQLite ({args.db})"

    def backend(method, path, body, headers, query):
        return client.raw(method, path, body, headers, query)

    namespace = args.namespace or ("demo-aws" if args.remote else "demo-local")
    server = ThreadingHTTPServer(("127.0.0.1", args.port), make_handler(backend, label, namespace))
    print(f"Dashboard: http://127.0.0.1:{args.port}/  backend: {label}")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
