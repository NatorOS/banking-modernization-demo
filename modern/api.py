"""HTTP routing shared by the local server and the AWS Lambda handler."""
import json
import re
import tempfile
import time
from pathlib import Path
from urllib.parse import parse_qs

from .reconciliation import ROOT, STAGES, build_report
from .service import PENDING, SUBMITTED, ServiceError, ValidationError, validate_namespace

MAX_BODY_BYTES = 16 * 1024
FIXTURE = ROOT / "fixtures" / "payments.json"
NS = r"(?P<ns>[^/]+)"
ROUTES = [
    ("GET", r"/api/health", "health"),
    ("GET", r"/api/legacy/baseline", "legacy_baseline"),
    ("GET", rf"/api/namespaces/{NS}/state", "state"),
    ("GET", rf"/api/namespaces/{NS}/reconciliation", "reconciliation"),
    ("POST", rf"/api/namespaces/{NS}/reset", "reset"),
    ("POST", rf"/api/namespaces/{NS}/purge", "purge"),
    ("POST", rf"/api/namespaces/{NS}/payments", "submit"),
    ("POST", rf"/api/namespaces/{NS}/events", "events"),
    ("POST", rf"/api/namespaces/{NS}/reconcile-pending", "reconcile_pending"),
    ("POST", rf"/api/namespaces/{NS}/demo/(?P<action>[a-z0-9-]+)", "demo"),
]


def fixture():
    return json.loads(FIXTURE.read_text())


def fixture_key(payment_id):
    return f"fixture-{payment_id}"


def run_legacy_baseline():
    from legacy.batch import connect, run_batch, seed
    with tempfile.TemporaryDirectory() as tmp:
        with connect(Path(tmp) / "legacy.sqlite") as conn:
            seed(conn)
            result = run_batch(conn)
        conn.close()
    golden = json.loads((ROOT / "fixtures" / "golden_batch.json").read_text())
    return {"source": "legacy/batch.py (unchanged), fresh SQLite", "result": result, "golden": golden,
            "matches_golden": result == golden}


def _error(exc):
    return {"error": {"code": exc.code, "message": exc.message, "details": exc.details}}


class App:
    def __init__(self, service, provider, environment="local"):
        self.service = service
        self.provider = provider
        self.environment = environment

    def handle(self, method, path, headers=None, body=b"", query=""):
        """Return (status, response_headers, response_body_bytes, route_name)."""
        headers = {k.lower(): v for k, v in (headers or {}).items()}
        route, match = None, None
        for m, pattern, name in ROUTES:
            found = re.fullmatch(pattern, path)
            if found and m == method:
                route, match = name, found
                break
            if found:
                route = route or "method_not_allowed"
        if route is None:
            return self._json(404, {"error": {"code": "not_found", "message": "no such route"}}, "not_found")
        if route == "method_not_allowed":
            return self._json(405, {"error": {"code": "method_not_allowed", "message": method}}, route)
        if len(body or b"") > MAX_BODY_BYTES:
            return self._json(413, {"error": {"code": "body_too_large", "message": "max 16 KiB"}}, route)
        try:
            params = match.groupdict()
            if "ns" in params:
                validate_namespace(params["ns"])
            status, payload = getattr(self, "_" + route)(params, headers, body or b"", parse_qs(query or ""))
        except ServiceError as exc:
            status, payload = exc.status, _error(exc)
        return self._json(status, payload, route)

    @staticmethod
    def _json(status, payload, route):
        return status, {"content-type": "application/json", "cache-control": "no-store"}, \
            (json.dumps(payload, indent=2, sort_keys=False) + "\n").encode(), route

    @staticmethod
    def _body(body):
        if not body:
            return {}
        try:
            return json.loads(body)
        except (ValueError, UnicodeDecodeError):
            raise ValidationError("request body must be valid JSON") from None

    # Routes --------------------------------------------------------------------------------

    def _health(self, params, headers, body, query):
        return 200, {"ok": True, "environment": self.environment, "store": self.service.store.backend,
                     "provider": {"name": self.provider.name, "mode": self.provider.mode},
                     "data_classification": "synthetic"}

    def _legacy_baseline(self, params, headers, body, query):
        return 200, run_legacy_baseline()

    def _state(self, params, headers, body, query):
        ns = params["ns"]
        snapshot = self.service.snapshot(ns)
        return 200, {"snapshot": snapshot, "reconciliation": build_report(snapshot, self.provider),
                     "provider": {"name": self.provider.name, "mode": self.provider.mode},
                     "store": self.service.store.backend, "environment": self.environment,
                     "process_usage": dict(self.service.store.usage)}

    def _reconciliation(self, params, headers, body, query):
        stage = (query.get("stage") or [None])[0]
        if stage is not None and stage not in STAGES:
            raise ValidationError(f"stage must be one of {', '.join(STAGES)}")
        return 200, build_report(self.service.snapshot(params["ns"]), self.provider, stage)

    def _reset(self, params, headers, body, query):
        data = self._body(body)
        opening = data.get("opening_cash_cents", fixture()["opening_cash_cents"])
        return 200, self.service.reset(params["ns"], opening, data.get("currency", "USD"))

    def _purge(self, params, headers, body, query):
        if self._body(body).get("confirm") != params["ns"]:
            raise ValidationError("purge requires {\"confirm\": \"<namespace>\"}")
        return 200, self.service.purge(params["ns"])

    def _submit(self, params, headers, body, query):
        result = self.service.submit_payment(params["ns"], headers.get("idempotency-key"), self._body(body))
        return self._submission_status(result), result

    @staticmethod
    def _submission_status(result):
        if result["replayed"] and result["payment"] and result["payment"]["status"] not in (PENDING,):
            return 200
        return 202 if result["payment"] is None or result["payment"]["status"] == PENDING else 201

    def _events(self, params, headers, body, query):
        return 200, self.service.ingest_webhook(params["ns"], body, headers)

    def _reconcile_pending(self, params, headers, body, query):
        return 200, self.service.reconcile_pending(params["ns"])

    # Demo actions (simulator only) ----------------------------------------------------------

    def _deliver(self, ns, event):
        body, signed = self.provider.signed_delivery(event)
        try:
            return {"event_id": event.get("event_id"), "http_status": 200,
                    **self.service.ingest_webhook(ns, body, signed)}
        except ServiceError as exc:
            return {"event_id": event.get("event_id"), "http_status": exc.status, **_error(exc)}

    def _provider_event(self, ns, payment_id, kind, variant=0, return_code="R01"):
        payment = self.service.get_payment(ns, payment_id)
        if payment is None:
            raise ValidationError(f"no payment {payment_id} in the active run")
        key = payment["provider_idempotency_key"]
        try:
            event = (self.provider.settlement_event(ns, key, variant) if kind == "settled"
                     else self.provider.return_event(ns, key, return_code, variant))
        except LookupError:
            raise ValidationError(f"simulator has no record of {payment_id}") from None
        return self._deliver(ns, event)

    def _demo(self, params, headers, body, query):
        if getattr(self.provider, "name", None) != "simulator":
            raise ValidationError("demo actions require the deterministic simulator")
        ns, action, data = params["ns"], params["action"], self._body(body)
        results = []
        if action == "submit-fixtures":
            for p in fixture()["payments"]:
                request = {"payment_id": p["id"], "beneficiary": p["beneficiary"],
                           "amount_cents": p["amount_cents"], "currency": fixture()["currency"]}
                try:
                    result = self.service.submit_payment(ns, fixture_key(p["id"]), request)
                    results.append({"http_status": self._submission_status(result), **result})
                except ServiceError as exc:
                    results.append({"payment_id": p["id"], "http_status": exc.status, **_error(exc)})
        elif action == "settle-all":
            for p in self.service.snapshot(ns)["payments"]:
                if p["status"] in (PENDING, SUBMITTED) and \
                        self.provider.lookup(ns, p["provider_idempotency_key"]) is not None:
                    results.append(self._provider_event(ns, p["payment_id"], "settled"))
        elif action == "replay-settlement":
            results.append(self._redeliver_for(ns, data.get("payment_id", "PAY-001"), "settled"))
        elif action == "duplicate-settlement":
            results.append(self._provider_event(ns, data.get("payment_id", "PAY-001"), "settled",
                                                variant=int(data.get("variant", 2))))
        elif action == "return-pay-002":
            results.append(self._provider_event(ns, "PAY-002", "returned", return_code="R01"))
        elif action == "replay-return":
            results.append(self._redeliver_for(ns, data.get("payment_id", "PAY-002"), "returned"))
        elif action == "provider-event":
            kind = data.get("type")
            if kind not in ("settled", "returned"):
                raise ValidationError("type must be settled or returned")
            results.append(self._provider_event(ns, data.get("payment_id"), kind, int(data.get("variant", 0)),
                                                data.get("return_code", "R01")))
        elif action == "redeliver":
            event = self.provider.emitted_event(ns, str(data.get("event_id")))
            if event is None:
                raise ValidationError("simulator never emitted that event_id")
            results.append(self._deliver(ns, event))
        elif action == "deliver-event":
            if not isinstance(data.get("event"), dict):
                raise ValidationError("deliver-event requires an event object")
            results.append(self._deliver(ns, data["event"]))
        elif action == "arm-timeout":
            count = data.get("count", 1)
            if type(count) is not int or not 1 <= count <= 10:
                raise ValidationError("count must be 1..10")
            self.provider.arm_timeout_after_accept(ns, count)
            results.append({"armed_timeouts_after_accept": count})
        else:
            raise ValidationError(f"unknown demo action {action}")
        return 200, {"action": action, "namespace": ns, "results": results}

    def _redeliver_for(self, ns, payment_id, kind):
        payment = self.service.get_payment(ns, payment_id)
        if payment is None:
            raise ValidationError(f"no payment {payment_id} in the active run")
        ppid = self.provider.provider_payment_id(payment["provider_idempotency_key"])
        event = self.provider.emitted_event(ns, f"evt_{ppid}_{kind}")
        if event is None:
            raise ValidationError(f"no {kind} event has been emitted for {payment_id} yet")
        return self._deliver(ns, event)


def timed(app, method, path, headers=None, body=b"", query=""):
    start = time.perf_counter()
    status, response_headers, payload, route = app.handle(method, path, headers, body, query)
    return status, response_headers, payload, route, round((time.perf_counter() - start) * 1000, 2)
