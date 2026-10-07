"""Vercel Function: SigV4-signs the dashboard's /api calls and forwards them to the IAM-protected HTTP API.

vercel.json rewrites /api/* here. AWS credentials come from Vercel OIDC: the per-request token is
exchanged (sts:AssumeRoleWithWebIdentity) for short-lived credentials of the role defined in
infra/vercel-access.json, so no AWS keys are stored in Vercel. Only the dashboard's routes are
forwarded; purge and raw payment/event ingestion are not reachable through this proxy.

Environment: DEMO_API_URL (https://<api-id>.execute-api.<region>.amazonaws.com), AWS_ROLE_ARN,
DEMO_AWS_REGION (default us-east-1).
"""
import json
import os
import re
import threading
import time
import urllib.error
import urllib.request
from http.server import BaseHTTPRequestHandler
from urllib.parse import parse_qsl, urlencode, urlsplit

MAX_BODY_BYTES = 16 * 1024
NS = r"demo-[a-z0-9][a-z0-9-]{0,39}"
ROUTES = (
    ("GET", re.compile(r"/api/health")),
    ("GET", re.compile(r"/api/legacy/baseline")),
    ("GET", re.compile(rf"/api/namespaces/{NS}/(state|reconciliation)")),
    ("POST", re.compile(rf"/api/namespaces/{NS}/(reset|reconcile-pending|demo/[a-z0-9-]{{1,40}})")),
)
FORWARDED_HEADERS = ("content-type", "idempotency-key")
SELF_PATHS = ("/api/proxy", "/api/proxy.py")


def resolve(raw_path):
    """Return (api_path, query) for the original request, whether or not Vercel rewrote it to /api/proxy."""
    url = urlsplit(raw_path)
    params = parse_qsl(url.query, keep_blank_values=True)
    if url.path in SELF_PATHS:
        rest = [v for k, v in params if k == "path"]
        params = [(k, v) for k, v in params if k != "path"]
        path = "/api/" + rest[0].lstrip("/") if rest else url.path
    else:
        path = url.path
    return path, urlencode(params)


def allowed(method, path):
    return any(method == m and pattern.fullmatch(path) for m, pattern in ROUTES)


class RoleCredentials:
    """Caches the assumed-role credentials until shortly before they expire."""

    def __init__(self, role_arn, region, sts=None):
        self.role_arn, self.region, self._sts = role_arn, region, sts
        self._lock = threading.Lock()
        self._cached, self._expires = None, 0.0

    def _client(self):
        if self._sts is None:
            import botocore.session
            from botocore import UNSIGNED
            from botocore.config import Config
            self._sts = botocore.session.get_session().create_client(
                "sts", region_name=self.region, config=Config(signature_version=UNSIGNED))
        return self._sts

    def get(self, token):
        with self._lock:
            if self._cached is None or self._expires - time.time() < 120:
                from botocore.credentials import Credentials
                response = self._client().assume_role_with_web_identity(
                    RoleArn=self.role_arn, RoleSessionName="vercel-dashboard",
                    WebIdentityToken=token, DurationSeconds=900)
                c = response["Credentials"]
                self._cached = Credentials(c["AccessKeyId"], c["SecretAccessKey"], c["SessionToken"])
                self._expires = c["Expiration"].timestamp()
            return self._cached


def signed_request(base_url, region, credentials, method, path, query, body, headers):
    from botocore.auth import SigV4Auth
    from botocore.awsrequest import AWSRequest
    url = base_url.rstrip("/") + path + (("?" + query) if query else "")
    if urlsplit(url).scheme != "https":
        raise ValueError("DEMO_API_URL must use https")
    request = AWSRequest(method=method, url=url, data=body or None, headers=dict(headers))
    SigV4Auth(credentials, "execute-api", region).add_auth(request)
    return urllib.request.Request(url, data=body or None, method=method, headers=dict(request.headers.items()))


def send(request):
    try:
        with urllib.request.urlopen(request, timeout=25) as response:
            return response.status, response.read()
    except urllib.error.HTTPError as exc:
        return exc.code, exc.read()


_credentials = None
_credentials_lock = threading.Lock()


def credentials_for(token):
    global _credentials
    with _credentials_lock:
        if _credentials is None:
            _credentials = RoleCredentials(os.environ["AWS_ROLE_ARN"], os.environ.get("DEMO_AWS_REGION", "us-east-1"))
    return _credentials.get(token)


def error(status, code, message):
    return status, json.dumps({"error": {"code": code, "message": message}}).encode()


def proxy(method, raw_path, headers, body):
    path, query = resolve(raw_path)
    if not allowed(method, path):
        return error(404, "not_proxied", "route is not available through the hosted dashboard")
    if method == "POST" and not (headers.get("content-type") or "").startswith("application/json"):
        return error(415, "unsupported_media_type", "POST bodies must be application/json")
    base_url, role_arn = os.environ.get("DEMO_API_URL"), os.environ.get("AWS_ROLE_ARN")
    if not base_url or not role_arn:
        return error(500, "not_configured", "set DEMO_API_URL and AWS_ROLE_ARN on the Vercel project")
    token = headers.get("x-vercel-oidc-token") or os.environ.get("VERCEL_OIDC_TOKEN")
    if not token:
        return error(500, "no_oidc_token", "enable OIDC federation on the Vercel project")
    region = os.environ.get("DEMO_AWS_REGION", "us-east-1")
    try:
        credentials = credentials_for(token)
    except Exception as exc:  # STS errors never include the token; log only the type.
        print(json.dumps({"event": "assume_role_failed", "error": type(exc).__name__}))
        return error(502, "credentials_unavailable", "could not assume the dashboard role")
    forwarded = {k: headers[k] for k in FORWARDED_HEADERS if headers.get(k)}
    return send(signed_request(base_url, region, credentials, method, path, query, body, forwarded))


class handler(BaseHTTPRequestHandler):
    def _dispatch(self, method):
        length = int(self.headers.get("content-length") or 0)
        if length > MAX_BODY_BYTES:
            status, payload = error(413, "body_too_large", "max 16 KiB")
        else:
            body = self.rfile.read(length) if length else b""
            headers = {k.lower(): v for k, v in self.headers.items()}
            status, payload = proxy(method, self.path, headers, body)
        self.send_response(status)
        self.send_header("content-type", "application/json")
        self.send_header("cache-control", "no-store")
        self.send_header("content-length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)

    def do_GET(self):
        self._dispatch("GET")

    def do_POST(self):
        self._dispatch("POST")
