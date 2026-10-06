"""AWS Lambda entry point for API Gateway HTTP API (payload format 2.0)."""
import base64
import json
import time

from .api import timed
from .config import build_app

_APP = None


def _app():
    global _APP
    if _APP is None:
        _APP = build_app()
    return _APP


def handler(event, context):
    http = event.get("requestContext", {}).get("http", {})
    body = event.get("body") or ""
    body = base64.b64decode(body) if event.get("isBase64Encoded") else body.encode()
    status, headers, payload, route, ms = timed(_app(), http.get("method", "GET"), event.get("rawPath", "/"),
                                                event.get("headers") or {}, body, event.get("rawQueryString", ""))
    # Structured access log: no bodies, headers, signatures or identities beyond the request id.
    print(json.dumps({"level": "INFO", "msg": "request", "route": route, "method": http.get("method"),
                      "status": status, "latency_ms": ms,
                      "request_id": event.get("requestContext", {}).get("requestId"), "ts": int(time.time())}))
    return {"statusCode": status, "headers": headers, "body": payload.decode()}
