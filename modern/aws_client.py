"""SigV4-signed client for the IAM-protected HTTP API (requires botocore, e.g. `pip install boto3`)."""
import json
import urllib.error
import urllib.request
from urllib.parse import urlsplit


class SignedApiClient:
    def __init__(self, base_url, profile=None, region="us-east-1"):
        import botocore.session
        from botocore.auth import SigV4Auth
        self.base_url = base_url.rstrip("/")
        self.region = region
        session = botocore.session.Session(profile=profile)
        credentials = session.get_credentials()
        if credentials is None:
            raise SystemExit("No AWS credentials for the selected profile; run `aws login --profile ...` first.")
        self._auth = SigV4Auth(credentials, "execute-api", region)

    def raw(self, method, path, body=b"", headers=None, query=""):
        from botocore.awsrequest import AWSRequest
        url = self.base_url + path + (("?" + query) if query else "")
        headers = dict(headers or {})
        if body:
            headers.setdefault("content-type", "application/json")
        request = AWSRequest(method=method, url=url, data=body or None, headers=headers)
        self._auth.add_auth(request)
        prepared = urllib.request.Request(url, data=body or None, method=method, headers=dict(request.headers.items()))
        if urlsplit(url).scheme != "https":
            raise SystemExit("Remote API URL must use https.")
        try:
            with urllib.request.urlopen(prepared, timeout=30) as response:
                return response.status, response.read()
        except urllib.error.HTTPError as exc:
            return exc.code, exc.read()

    def request(self, method, path, payload=None, headers=None, query=""):
        body = json.dumps(payload).encode() if payload is not None else b""
        status, raw = self.raw(method, path, body, headers, query)
        try:
            return status, json.loads(raw or b"{}")
        except ValueError:
            return status, {"raw": raw.decode(errors="replace")}


class LocalClient:
    """Same interface as SignedApiClient, routed in-process through the shared App."""

    def __init__(self, app):
        self.app = app

    def raw(self, method, path, body=b"", headers=None, query=""):
        status, _, payload, _ = self.app.handle(method, path, headers or {}, body, query)
        return status, payload

    def request(self, method, path, payload=None, headers=None, query=""):
        body = json.dumps(payload).encode() if payload is not None else b""
        status, raw = self.raw(method, path, body, headers, query)
        return status, json.loads(raw)
