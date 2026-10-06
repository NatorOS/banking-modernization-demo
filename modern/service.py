"""Payment subledger service: submission idempotency, provider events and journal posting.

See docs/DESIGN.md for the state machine and ledger semantics. All monetary amounts are integer
cents. Every state change is a single conditional transaction (see modern/store.py) that pins
the active run with a Check on the namespace META item.
"""
import hashlib
import json
import random
import re
import time
import uuid
from datetime import datetime, timedelta, timezone

from .provider import ProviderRejected, ProviderSubmission, ProviderTimeout, ProviderUnavailable
from .store import Check, ConditionFailed, Contention, Increment, Put, Replace, merge_increments
from .webhook import SignatureError

PENDING, SUBMITTED, SETTLED, RETURNED, REJECTED = (
    "PENDING_SUBMISSION", "SUBMITTED", "SETTLED", "RETURNED", "REJECTED")
STATUSES = (PENDING, SUBMITTED, SETTLED, RETURNED, REJECTED)

# Allowed transitions. Anything else is refused; terminal states are RETURNED and REJECTED.
TRANSITIONS = {
    (PENDING, SUBMITTED): "provider accepted the submission",
    (PENDING, REJECTED): "provider definitively rejected; reservation released",
    (PENDING, SETTLED): "settlement event arrived before the acceptance response was recorded",
    (PENDING, RETURNED): "settlement event arrived with a parked return",
    (SUBMITTED, SETTLED): "settlement event",
    (SUBMITTED, RETURNED): "settlement event arrived with a parked return",
    (SETTLED, RETURNED): "return event; compensating journal entry",
}

# Event outcomes recorded in delivery history. Nothing is dropped silently.
APPLIED, PARKED, DUPLICATE_EVENT, DUPLICATE_EFFECT, REJECTED_EVENT = (
    "APPLIED", "PARKED", "DUPLICATE_EVENT", "DUPLICATE_EFFECT", "REJECTED")

RETURN_CODES = {"R01": "insufficient funds", "R02": "account closed", "R03": "no account",
                "R04": "invalid account number"}
EVENT_TYPES = ("payment.settled", "payment.returned")

NAMESPACE_RE = re.compile(r"^demo-[a-z0-9][a-z0-9-]{0,39}$")
PAYMENT_ID_RE = re.compile(r"^[A-Z0-9][A-Z0-9-]{2,39}$")
IDEMPOTENCY_KEY_RE = re.compile(r"^[A-Za-z0-9_.:-]{8,128}$")
EVENT_ID_RE = re.compile(r"^[A-Za-z0-9_.:-]{4,128}$")
PROVIDER_ID_RE = re.compile(r"^[A-Za-z0-9_.:-]{4,128}$")
MAX_AMOUNT_CENTS = 10_000_000
SUPPORTED_CURRENCIES = ("USD",)


class ServiceError(Exception):
    status = 400
    code = "bad_request"

    def __init__(self, message, code=None, details=None):
        super().__init__(message)
        self.message = message
        self.code = code or self.code
        self.details = details or {}


class ValidationError(ServiceError):
    status, code = 400, "validation_error"


class Unauthorized(ServiceError):
    status, code = 401, "unauthorized"


class NotFound(ServiceError):
    status, code = 404, "not_found"


class IdempotencyConflict(ServiceError):
    status, code = 409, "idempotency_key_conflict"


class PaymentConflict(ServiceError):
    status, code = 409, "payment_reference_conflict"


class EventConflict(ServiceError):
    status, code = 409, "event_id_conflict"


class InsufficientFunds(ServiceError):
    status, code = 422, "insufficient_funds"


class RejectedEvent(ServiceError):
    status, code = 422, "event_rejected"


class ServiceBusy(ServiceError):
    status, code = 503, "retry_later"


def canonical(obj):
    return json.dumps(obj, sort_keys=True, separators=(",", ":"), ensure_ascii=True)


def sha256(text):
    return hashlib.sha256(text.encode()).hexdigest()


def digest(obj):
    return sha256(canonical(obj))


def iso(dt):
    return dt.astimezone(timezone.utc).isoformat(timespec="microseconds").replace("+00:00", "Z")


def parse_iso(value):
    return datetime.fromisoformat(value.replace("Z", "+00:00"))


def strip(item):
    return {k: v for k, v in item.items() if k not in ("pk", "sk", "_v")}


def validate_namespace(namespace):
    if not isinstance(namespace, str) or not NAMESPACE_RE.match(namespace):
        raise ValidationError("namespace must match demo-[a-z0-9-] (synthetic demo namespaces only)",
                              code="invalid_namespace")
    return namespace


def validate_amount(value):
    if type(value) is not int or value <= 0 or value > MAX_AMOUNT_CENTS:
        raise ValidationError(f"amount_cents must be an integer between 1 and {MAX_AMOUNT_CENTS}",
                              code="invalid_amount")
    return value


def validate_currency(value):
    if value not in SUPPORTED_CURRENCIES:
        raise ValidationError("currency must be USD", code="invalid_currency")
    return value


def validate_payment_request(body):
    if not isinstance(body, dict):
        raise ValidationError("payment body must be a JSON object")
    expected = {"payment_id", "beneficiary", "amount_cents", "currency"}
    if set(body) != expected:
        raise ValidationError("payment body must contain exactly payment_id, beneficiary, amount_cents, currency",
                              details={"missing": sorted(expected - set(body)), "unexpected": sorted(set(body) - expected)})
    payment_id = body["payment_id"]
    if not isinstance(payment_id, str) or not PAYMENT_ID_RE.match(payment_id):
        raise ValidationError("payment_id must be 3-40 chars of A-Z, 0-9 and '-'", code="invalid_payment_reference")
    beneficiary = body["beneficiary"]
    if not isinstance(beneficiary, str) or not beneficiary.strip() or len(beneficiary) > 140 \
            or not beneficiary.isprintable():
        raise ValidationError("beneficiary must be 1-140 printable characters", code="invalid_beneficiary")
    return {"payment_id": payment_id, "beneficiary": beneficiary,
            "amount_cents": validate_amount(body["amount_cents"]), "currency": validate_currency(body["currency"])}


def validate_event(event):
    """Schema validation for provider events. Returns a list of problems (empty if valid)."""
    if not isinstance(event, dict):
        return ["event must be a JSON object"]
    problems = []
    required = {"event_id", "type", "provider", "provider_payment_id", "client_reference",
                "amount_cents", "currency", "occurred_at"}
    allowed = required | {"return_code"}
    if required - set(event):
        problems.append("missing fields: " + ", ".join(sorted(required - set(event))))
    if set(event) - allowed:
        problems.append("unexpected fields: " + ", ".join(sorted(set(event) - allowed)))
    if not isinstance(event.get("event_id"), str) or not EVENT_ID_RE.match(event.get("event_id") or ""):
        problems.append("invalid event_id")
    if event.get("type") not in EVENT_TYPES:
        problems.append("unsupported event type")
    if not isinstance(event.get("provider_payment_id"), str) or not PROVIDER_ID_RE.match(
            event.get("provider_payment_id") or ""):
        problems.append("invalid provider_payment_id")
    if not isinstance(event.get("client_reference"), str):
        problems.append("invalid client_reference")
    amount = event.get("amount_cents")
    if type(amount) is not int or amount <= 0 or amount > MAX_AMOUNT_CENTS:
        problems.append("invalid amount_cents")
    if event.get("currency") not in SUPPORTED_CURRENCIES:
        problems.append("invalid currency")
    if not isinstance(event.get("occurred_at"), str):
        problems.append("invalid occurred_at")
    if event.get("type") == "payment.returned" and event.get("return_code") not in RETURN_CODES:
        problems.append("return events require a known return_code")
    if event.get("type") == "payment.settled" and "return_code" in event:
        problems.append("settlement events must not carry return_code")
    return problems


class PaymentService:
    LEASE_SECONDS = 30
    MAX_ATTEMPTS = 8

    def __init__(self, store, provider, verifier=None, clock=None, sleep=time.sleep):
        self.store = store
        self.provider = provider
        self.verifier = verifier
        self.clock = clock or (lambda: datetime.now(timezone.utc))
        self.sleep = sleep

    # Keys -----------------------------------------------------------------------------------

    @staticmethod
    def pk(namespace):
        return f"NS#{namespace}"

    @staticmethod
    def prefix(run):
        return f"RUN#{run:05d}#"

    def _check_run(self, namespace, run):
        return Check(self.pk(namespace), "META", "run", run)

    def _backoff(self, attempt):
        self.sleep(min(0.2, 0.005 * (2 ** attempt)) * random.uniform(0.5, 1.0))

    def provider_key(self, namespace, run, payment_id):
        return f"{namespace}:{run}:{payment_id}"

    @staticmethod
    def item_run(item):
        """Run generation an item belongs to, or None if it is not generation-scoped."""
        sk = item["sk"]
        if sk.startswith("RUN#"):
            return int(sk.split("#", 2)[1])
        if sk.startswith("SIM#PAYMENT#"):
            ref = sk[len("SIM#PAYMENT#"):]
        elif sk.startswith("SIM#EVENT#"):
            payload = item.get("payload")
            ref = payload.get("client_reference") if isinstance(payload, dict) else None
        else:
            return None
        parts = ref.split(":") if isinstance(ref, str) else []
        return int(parts[1]) if len(parts) == 3 and parts[1].isdigit() else None

    # Namespace lifecycle ---------------------------------------------------------------------

    def meta(self, namespace):
        validate_namespace(namespace)
        meta = self.store.get(self.pk(namespace), "META")
        if meta is None or meta.get("purged"):
            raise NotFound("namespace has not been initialised; reset it first", code="unknown_namespace")
        return meta

    def _balance_item(self, opening_cash_cents, currency):
        return {"opening_cash_cents": opening_cash_cents, "cash_cents": opening_cash_cents,
                "reserved_cents": 0, "available_cents": opening_cash_cents, "currency": currency}

    def reset(self, namespace, opening_cash_cents=100000, currency="USD"):
        """Start a fresh run generation in this synthetic namespace only.

        The new run gets new keys (``RUN#n#``) and new provider idempotency keys, and every
        write pins the run, so delayed events or in-flight submission responses from older
        runs can no longer change state. Items of strictly older runs are then purged, so an
        overlapping reset that already started a newer run is never deleted; the simulator's
        own (provider-side) history is kept. Other namespaces are never touched.
        """
        validate_namespace(namespace)
        validate_amount(opening_cash_cents)
        validate_currency(currency)
        pk = self.pk(namespace)
        for attempt in range(self.MAX_ATTEMPTS):
            meta = self.store.get(pk, "META")
            now = iso(self.clock())
            run = (meta["run"] + 1) if meta else 1
            new_meta = {"namespace": namespace, "run": run, "currency": currency,
                        "opening_cash_cents": opening_cash_cents, "data_classification": "synthetic",
                        "created_at": meta["created_at"] if meta else now, "run_started_at": now}
            meta_op = Replace(pk, "META", new_meta, meta["_v"]) if meta else Put(pk, "META", new_meta)
            try:
                self.store.transact([meta_op, Put(pk, self.prefix(run) + "BALANCE",
                                                  self._balance_item(opening_cash_cents, currency))])
            except (ConditionFailed, Contention):
                self._backoff(attempt)
                continue
            purged = self.store.delete_prefix(pk, "RUN#", where=lambda item, run=run: self.item_run(item) < run)
            return {"namespace": namespace, "run": run, "previous_run": meta["run"] if meta else None,
                    "purged_previous_run_items": purged}
        raise ServiceBusy("reset contended; retry")

    def ensure_namespace(self, namespace, opening_cash_cents=100000, currency="USD"):
        validate_namespace(namespace)
        meta = self.store.get(self.pk(namespace), "META")
        if meta is None or meta.get("purged"):
            try:
                return self.reset(namespace, opening_cash_cents, currency)
            except ServiceBusy:
                pass
        return {"namespace": namespace, "run": self.meta(namespace)["run"]}

    def purge(self, namespace):
        """Delete this synthetic namespace's data, including simulator history.

        META becomes a tombstone that keeps the generation counter, fenced one past the last
        run: writes pinned to an older run fail their run check, and the next reset starts a
        generation whose provider references were never issued, so captured events stay stale.
        Only items up to the fence are deleted, so a reset racing the purge keeps its new run.
        """
        validate_namespace(namespace)
        pk = self.pk(namespace)
        for attempt in range(self.MAX_ATTEMPTS):
            meta = self.store.get(pk, "META")
            seen = [r for r in map(self.item_run, self.store.query(pk, "RUN#") + self.store.query(pk, "SIM#"))
                    if r is not None]
            fence = max([meta["run"] if meta else 0] + seen) + 1
            now = iso(self.clock())
            tombstone = {"namespace": namespace, "run": fence, "purged": True, "purged_at": now,
                         "data_classification": "synthetic", "created_at": meta["created_at"] if meta else now}
            try:
                self.store.transact([Replace(pk, "META", tombstone, meta["_v"]) if meta
                                     else Put(pk, "META", tombstone)])
            except (ConditionFailed, Contention):
                self._backoff(attempt)
                continue

            def fenced(item, fence=fence):
                run = self.item_run(item)
                return run is None or run <= fence
            deleted = self.store.delete_prefix(pk, "RUN#", where=fenced) + \
                self.store.delete_prefix(pk, "SIM#", where=fenced)
            return {"namespace": namespace, "deleted_items": deleted, "next_run": fence + 1}
        raise ServiceBusy("purge contended; retry")

    # Views ------------------------------------------------------------------------------------

    @staticmethod
    def payment_view(payment):
        keys = ("payment_id", "beneficiary", "amount_cents", "currency", "status", "idempotency_key",
                "provider_payment_id", "provider_idempotency_key", "created_at", "updated_at",
                "last_submission_outcome", "submission_attempts", "pending_return", "settlement_event_id",
                "return_event_id", "return_code", "history", "run")
        view = {k: payment.get(k) for k in keys}
        view["lease_active"] = bool(payment.get("lease_owner"))
        return view

    def snapshot(self, namespace):
        meta = self.meta(namespace)
        run = meta["run"]
        items = self.store.query(self.pk(namespace), self.prefix(run))
        groups = {"BALANCE": [], "PAYMENT": [], "IDEM": [], "EVENT": [], "JOURNAL": [], "DELIVERY": []}
        for item in items:
            kind = item["sk"][len(self.prefix(run)):].split("#", 1)[0]
            if kind in groups:
                groups[kind].append(item)
        balance = strip(groups["BALANCE"][0]) if groups["BALANCE"] else None
        return {
            "namespace": namespace, "run": run, "meta": strip(meta), "balance": balance,
            "payments": [strip(p) for p in sorted(groups["PAYMENT"], key=lambda p: p["payment_id"])],
            "idempotency_records": [strip(i) for i in groups["IDEM"]],
            "events": [strip(e) for e in groups["EVENT"]],
            "journal": [strip(j) for j in sorted(groups["JOURNAL"], key=lambda j: (j["posted_at"], j["entry_id"]))],
            "deliveries": [strip(d) for d in sorted(groups["DELIVERY"], key=lambda d: d["sk"])],
        }

    def get_payment(self, namespace, payment_id):
        run = self.meta(namespace)["run"]
        return self.store.get(self.pk(namespace), self.prefix(run) + "PAYMENT#" + payment_id)

    # Submission ------------------------------------------------------------------------------

    def submit_payment(self, namespace, idempotency_key, body):
        """Create (or idempotently replay) a payment submission.

        New request: one transaction claims the idempotency key, creates the payment in
        PENDING_SUBMISSION and reserves funds (available -= amount, reserved += amount, guarded
        so neither goes negative). Only then is the provider called.
        """
        if not isinstance(idempotency_key, str) or not IDEMPOTENCY_KEY_RE.match(idempotency_key):
            raise ValidationError("Idempotency-Key header must be 8-128 chars of [A-Za-z0-9_.:-]",
                                  code="invalid_idempotency_key")
        request = validate_payment_request(body)
        request_canonical = canonical(request)
        request_hash = digest(request)
        pk = self.pk(namespace)
        for attempt in range(self.MAX_ATTEMPTS):
            meta = self.meta(namespace)
            run, prefix = meta["run"], self.prefix(meta["run"])
            idem = self.store.get(pk, prefix + "IDEM#" + idempotency_key)
            if idem is not None:
                if idem["payload_hash"] != request_hash:
                    raise IdempotencyConflict("Idempotency-Key was already used with a different payload",
                                              details={"payment_id": idem["payment_id"]})
                payment = self.store.get(pk, prefix + "PAYMENT#" + idem["payment_id"])
                return self._drive(namespace, run, payment, replayed=True)
            if request["currency"] != meta["currency"]:
                raise ValidationError("currency does not match the namespace currency", code="invalid_currency")
            now = iso(self.clock())
            amount = request["amount_cents"]
            payment = dict(request, status=PENDING, run=run, idempotency_key=idempotency_key,
                           payload_hash=request_hash,
                           provider_idempotency_key=self.provider_key(namespace, run, request["payment_id"]),
                           provider_payment_id=None, created_at=now, updated_at=now, lease_owner=None,
                           lease_until=None, submission_attempts=0, last_submission_outcome=None,
                           pending_return=None,
                           history=[{"at": now, "status": PENDING, "reason": "funds reserved"}])
            ops = [
                self._check_run(namespace, run),
                Put(pk, prefix + "IDEM#" + idempotency_key, {
                    "idempotency_key": idempotency_key, "payment_id": request["payment_id"],
                    "payload_hash": request_hash, "canonical_payload": request_canonical, "created_at": now}),
                Put(pk, prefix + "PAYMENT#" + request["payment_id"], payment),
                Increment(pk, prefix + "BALANCE", {"available_cents": -amount, "reserved_cents": amount}),
            ]
            try:
                self.store.transact(ops)
            except ConditionFailed as exc:
                if exc.index in (0, 1):  # reset raced us, or a concurrent request claimed the key
                    self._backoff(attempt)
                    continue
                # Real DynamoDB can report the payment/balance condition first when a
                # competing transaction claimed this same key. Re-read the durable claim
                # before interpreting that cancellation as a business conflict or NSF.
                if self.store.get(pk, prefix + "IDEM#" + idempotency_key) is not None:
                    self._backoff(attempt)
                    continue
                if exc.index == 2:
                    raise PaymentConflict("payment_id already exists under a different Idempotency-Key",
                                          details={"payment_id": request["payment_id"]}) from None
                balance = self.store.get(pk, prefix + "BALANCE")
                raise InsufficientFunds("insufficient available cash for this payment", details={
                    "available_cents": balance and balance["available_cents"], "amount_cents": amount}) from None
            except Contention:
                self._backoff(attempt)
                continue
            payment.update(pk=pk, sk=prefix + "PAYMENT#" + request["payment_id"], _v=1)
            return self._drive(namespace, run, payment, replayed=False)
        raise ServiceBusy("submission contended; retry with the same Idempotency-Key")

    def _result(self, payment, replayed, outcome):
        return {"payment": self.payment_view(payment), "replayed": replayed, "outcome": outcome}

    def _lease_active(self, payment):
        return bool(payment.get("lease_owner")) and parse_iso(payment["lease_until"]) > self.clock()

    def _drive(self, namespace, run, payment, replayed, lookup_first=False):
        if payment["status"] != PENDING:
            return self._result(payment, replayed, "existing" if replayed else payment["status"].lower())
        if self._lease_active(payment):
            return self._result(payment, replayed, "submission_in_progress")
        pk, sk = payment["pk"], payment["sk"]
        now = self.clock()
        owner = uuid.uuid4().hex
        leased = strip(payment) | {"lease_owner": owner, "lease_until": iso(now + timedelta(seconds=self.LEASE_SECONDS)),
                                   "submission_attempts": payment.get("submission_attempts", 0) + 1,
                                   "updated_at": iso(now)}
        try:
            self.store.transact([self._check_run(namespace, run), Replace(pk, sk, leased, payment["_v"])])
        except (ConditionFailed, Contention):
            current = self.store.get(pk, sk)
            if current is None:
                raise NotFound("payment no longer exists in the active run", code="stale_run") from None
            if current["status"] != PENDING or self._lease_active(current):
                return self._result(current, replayed, "existing" if current["status"] != PENDING
                                    else "submission_in_progress")
            return self._drive(namespace, run, current, replayed, lookup_first)
        leased.update(pk=pk, sk=sk, _v=payment["_v"] + 1)
        submission = ProviderSubmission(namespace, payment["provider_idempotency_key"],
                                        payment["provider_idempotency_key"], payment["amount_cents"],
                                        payment["currency"], payment["beneficiary"])
        try:
            found = self.provider.lookup(namespace, submission.idempotency_key) if lookup_first else None
            outcome = ("accepted", found or self.provider.submit(submission))
        except ProviderRejected as exc:
            outcome = ("rejected", exc.reason)
        except (ProviderTimeout, ProviderUnavailable) as exc:
            outcome = ("unknown", str(exc) or type(exc).__name__)
        return self._record_outcome(namespace, run, leased, owner, outcome, replayed)

    def _record_outcome(self, namespace, run, payment, owner, outcome, replayed):
        """Apply a provider response only if we still own the lease and nothing else changed it.

        Late responses (after a settlement event, a newer worker, or a reset) are ignored:
        they can never downgrade SETTLED/RETURNED or release a reservation twice.
        """
        kind, detail = outcome
        pk, sk = payment["pk"], payment["sk"]
        prefix = self.prefix(run)
        for attempt in range(self.MAX_ATTEMPTS):
            if payment["status"] != PENDING or payment.get("lease_owner") != owner:
                return self._result(payment, replayed, "late_provider_response_ignored")
            now = iso(self.clock())
            new = strip(payment) | {"lease_owner": None, "lease_until": None, "updated_at": now}
            ops = [self._check_run(namespace, run)]
            if kind == "accepted":
                new.update(status=SUBMITTED, provider_payment_id=detail.provider_payment_id,
                           last_submission_outcome="ACCEPTED",
                           history=payment["history"] + [{"at": now, "status": SUBMITTED,
                                                          "reason": TRANSITIONS[(PENDING, SUBMITTED)]}])
                result = "submitted"
            elif kind == "rejected":
                new.update(status=REJECTED, last_submission_outcome="REJECTED: " + detail,
                           history=payment["history"] + [{"at": now, "status": REJECTED, "reason": detail}])
                ops.append(Increment(pk, prefix + "BALANCE", {"available_cents": payment["amount_cents"],
                                                              "reserved_cents": -payment["amount_cents"]}))
                result = "rejected"
            else:
                # Ambiguous: keep PENDING_SUBMISSION and keep the reservation. Retry or reconcile
                # with the same provider idempotency key; never treat as failed.
                new.update(last_submission_outcome="UNKNOWN: " + detail)
                result = "submission_outcome_unknown"
            ops.append(Replace(pk, sk, new, payment["_v"]))
            try:
                self.store.transact(ops)
                return self._result(new | {"pk": pk, "sk": sk}, replayed, result)
            except (ConditionFailed, Contention):
                self._backoff(attempt)
                payment = self.store.get(pk, sk)
                if payment is None:
                    return {"payment": None, "replayed": replayed, "outcome": "late_provider_response_ignored",
                            "reason": "run was reset"}
                if self.store.get(pk, "META")["run"] != run:
                    return self._result(payment, replayed, "late_provider_response_ignored")
        raise ServiceBusy("could not record provider outcome; reconcile-pending will resolve it")

    def reconcile_pending(self, namespace):
        """Explicit recovery for PENDING_SUBMISSION payments whose lease is free or expired.

        Looks the payment up at the provider by its namespace/run/payment-scoped idempotency key
        and, only if the provider has no record, resubmits with that same key.
        """
        meta = self.meta(namespace)
        results = []
        for payment in self.store.query(self.pk(namespace), self.prefix(meta["run"]) + "PAYMENT#"):
            if payment["status"] == PENDING:
                results.append(self._drive(namespace, meta["run"], payment, replayed=True, lookup_first=True))
        return {"namespace": namespace, "run": meta["run"], "results": results}

    # Provider events -------------------------------------------------------------------------

    def ingest_webhook(self, namespace, body, headers):
        """Verify a signed provider delivery, then apply it."""
        if self.verifier is None:
            raise Unauthorized("no webhook verifier configured")
        try:
            self.verifier.verify(body, headers)
        except SignatureError as exc:
            raise Unauthorized(f"webhook signature rejected: {exc}", code="invalid_signature") from None
        try:
            event = json.loads(body)
        except (ValueError, UnicodeDecodeError):
            self._record_rejection(namespace, None, "malformed JSON")
            raise RejectedEvent("event body is not valid JSON") from None
        return self.apply_event(namespace, event)

    def _delivery(self, namespace, run, event, outcome, reason, payment_id=None, related=None):
        now = self.clock()
        event_id = event.get("event_id") if isinstance(event, dict) else None
        # time_ns keeps arrival order stable when several deliveries share a timestamp.
        sk = f"{self.prefix(run)}DELIVERY#{iso(now)}#{time.time_ns():020d}#{uuid.uuid4().hex[:8]}"
        try:
            payload_json = canonical(event) if event is not None else None
        except (TypeError, ValueError):
            payload_json = None
        # Raw payloads are stored as canonical JSON text: rejected events may carry values (such
        # as fractional numbers) that DynamoDB's attribute types cannot represent.
        item = {"received_at": iso(now), "event_id": event_id if isinstance(event_id, str) else None,
                "type": event.get("type") if isinstance(event, dict) and isinstance(event.get("type"), str) else None,
                "payment_id": payment_id, "outcome": outcome, "reason": reason,
                "related_event_ids": related or [],
                "payload_hash": sha256(payload_json) if payload_json is not None else None,
                "payload_json": payload_json if payload_json is not None and len(payload_json) <= 4096 else None,
                "payload_bytes": len(payload_json) if payload_json is not None else None}
        return Put(self.pk(namespace), sk, item)

    def _record_rejection(self, namespace, event, reason, payment_id=None):
        meta = self.meta(namespace)
        try:
            self.store.transact([self._check_run(namespace, meta["run"]),
                                 self._delivery(namespace, meta["run"], event, REJECTED_EVENT, reason, payment_id)])
        except (ConditionFailed, Contention):
            pass  # audit-only write; the caller still receives the rejection

    def _reject(self, namespace, event, reason, payment_id=None, error=RejectedEvent):
        self._record_rejection(namespace, event, reason, payment_id)
        raise error(f"event rejected: {reason}", details={"reason": reason})

    def apply_event(self, namespace, event):
        problems = validate_event(event)
        if problems:
            self._reject(namespace, event, "; ".join(problems))
        event_hash = digest(event)
        pk = self.pk(namespace)
        for attempt in range(self.MAX_ATTEMPTS):
            meta = self.meta(namespace)
            run, prefix = meta["run"], self.prefix(meta["run"])
            existing = self.store.get(pk, prefix + "EVENT#" + event["event_id"])
            if existing is not None:
                if existing["payload_hash"] != event_hash:
                    self._reject(namespace, event, "event_id reused with different content",
                                 existing.get("payment_id"), EventConflict)
                ops = [self._check_run(namespace, run),
                       self._delivery(namespace, run, event, DUPLICATE_EVENT,
                                      f"event_id already processed with outcome {existing['outcome']}",
                                      existing.get("payment_id"))]
                try:
                    self.store.transact(ops)
                except (ConditionFailed, Contention):
                    self._backoff(attempt)
                    continue
                return {"outcome": DUPLICATE_EVENT, "original_outcome": existing["outcome"],
                        "event_id": event["event_id"], "payment_id": existing.get("payment_id")}

            # Resolve and validate against the payment before any duplicate-effect classification.
            ref = event["client_reference"].split(":")
            if len(ref) != 3 or ref[0] != namespace or not ref[1].isdigit():
                self._reject(namespace, event, "unknown client_reference")
            if int(ref[1]) != run:
                self._reject(namespace, event, f"stale_run: event belongs to run {ref[1]}, active run is {run}")
            payment = self.store.get(pk, prefix + "PAYMENT#" + ref[2])
            if payment is None or payment["provider_idempotency_key"] != event["client_reference"]:
                self._reject(namespace, event, "unknown payment reference")
            pid = payment["payment_id"]
            if payment.get("provider_payment_id") and payment["provider_payment_id"] != event["provider_payment_id"]:
                self._reject(namespace, event, "provider_payment_id does not match the payment", pid)
            if event["amount_cents"] != payment["amount_cents"]:
                self._reject(namespace, event, "amount does not match the payment", pid)
            if event["currency"] != payment["currency"]:
                self._reject(namespace, event, "currency does not match the payment", pid)

            status = payment["status"]
            if status == REJECTED:
                self._reject(namespace, event, f"invalid transition: {event['type']} for a REJECTED payment", pid)
            if event["type"] == "payment.settled":
                if status in (PENDING, SUBMITTED):
                    plan = self._plan_settlement(namespace, run, payment, event, event_hash)
                else:
                    plan = self._plan_duplicate_effect(namespace, run, payment, event, event_hash,
                                                       f"payment already {status}; settlement already posted")
            else:
                if status == SETTLED:
                    plan = self._plan_return(namespace, run, payment, event, event_hash)
                elif status == RETURNED or payment.get("pending_return"):
                    plan = self._plan_duplicate_effect(namespace, run, payment, event, event_hash,
                                                       "return already applied or parked")
                else:
                    plan = self._plan_park(namespace, run, payment, event, event_hash)
            ops, result = plan
            try:
                self.store.transact(merge_increments(ops))
                return result
            except (ConditionFailed, Contention):
                self._backoff(attempt)
                continue
        raise ServiceBusy("event processing contended; redeliver the event")

    # Event plans: each returns (ops, result) for one atomic transaction ---------------------

    def _event_record(self, event, event_hash, payment_id, outcome, now, **extra):
        return dict({"event_id": event["event_id"], "type": event["type"], "payload": event,
                     "payload_hash": event_hash, "payment_id": payment_id, "outcome": outcome,
                     "received_at": now}, **extra)

    def _journal(self, namespace, run, payment, effect, event_id, now, **extra):
        amount, pid = payment["amount_cents"], payment["payment_id"]
        sides = (("DEBIT", "payment_outflow"), ("CREDIT", "cash")) if effect == "SETTLEMENT" else \
            (("DEBIT", "cash"), ("CREDIT", "payment_outflow"))
        ops = []
        for side, account in sides:
            entry_id = f"{pid}#{effect}#{side}"
            ops.append(Put(self.pk(namespace), self.prefix(run) + "JOURNAL#" + entry_id, dict({
                "entry_id": entry_id, "payment_id": pid, "effect": effect, "side": side, "account": account,
                "amount_cents": amount, "currency": payment["currency"], "event_id": event_id,
                "posted_at": now, "run": run}, **extra)))
        return ops

    def _plan_settlement(self, namespace, run, payment, event, event_hash):
        pk, prefix, now = self.pk(namespace), self.prefix(run), iso(self.clock())
        amount, pid, eid = payment["amount_cents"], payment["payment_id"], event["event_id"]
        new = strip(payment) | {
            "status": SETTLED, "provider_payment_id": payment.get("provider_payment_id") or event["provider_payment_id"],
            "lease_owner": None, "lease_until": None, "settlement_event_id": eid, "settled_at": now, "updated_at": now,
            "history": payment["history"] + [{"at": now, "status": SETTLED, "event_id": eid,
                                              "reason": TRANSITIONS[(payment["status"], SETTLED)]}]}
        # Settlement posts cash: cash -= amount, reserved -= amount; available unchanged.
        ops = [self._check_run(namespace, run),
               Increment(pk, prefix + "BALANCE", {"cash_cents": -amount, "reserved_cents": -amount}),
               *self._journal(namespace, run, payment, "SETTLEMENT", eid, now)]
        related, effects = [], ["SETTLEMENT"]
        parked = payment.get("pending_return")
        if parked:
            parked_event = self.store.get(pk, prefix + "EVENT#" + parked["event_id"])
            new.update(status=RETURNED, return_event_id=parked["event_id"], return_code=parked["return_code"],
                       returned_at=now, pending_return=None)
            new["history"] = new["history"] + [{"at": now, "status": RETURNED, "event_id": parked["event_id"],
                                                "reason": f"parked return applied with settlement {eid}"}]
            # Return: cash += amount, available += amount. Merged into the one BALANCE update.
            ops.append(Increment(pk, prefix + "BALANCE", {"cash_cents": amount, "available_cents": amount}))
            ops += self._journal(namespace, run, payment, "RETURN", parked["event_id"], now,
                                 resolved_with_settlement_event_id=eid)
            ops.append(Replace(pk, prefix + "EVENT#" + parked["event_id"], strip(parked_event) | {
                "outcome": APPLIED, "resolved_by_event_id": eid, "resolved_at": now}, parked_event["_v"]))
            related, effects = [parked["event_id"]], ["SETTLEMENT", "RETURN"]
        ops += [Replace(pk, prefix + "PAYMENT#" + pid, new, payment["_v"]),
                Put(pk, prefix + "EVENT#" + eid, self._event_record(event, event_hash, pid, APPLIED, now,
                                                                    effects=effects, applied_parked_event_ids=related)),
                self._delivery(namespace, run, event, APPLIED,
                               "settlement posted" + (f"; resolved parked return {related[0]}" if related else ""),
                               pid, related)]
        return ops, {"outcome": APPLIED, "event_id": eid, "payment_id": pid, "status": new["status"],
                     "effects": effects, "applied_parked_event_ids": related}

    def _plan_return(self, namespace, run, payment, event, event_hash):
        pk, prefix, now = self.pk(namespace), self.prefix(run), iso(self.clock())
        amount, pid, eid = payment["amount_cents"], payment["payment_id"], event["event_id"]
        new = strip(payment) | {"status": RETURNED, "return_event_id": eid, "return_code": event["return_code"],
                                "returned_at": now, "updated_at": now,
                                "history": payment["history"] + [{"at": now, "status": RETURNED, "event_id": eid,
                                                                  "reason": TRANSITIONS[(SETTLED, RETURNED)]}]}
        ops = [self._check_run(namespace, run),
               Increment(pk, prefix + "BALANCE", {"cash_cents": amount, "available_cents": amount}),
               *self._journal(namespace, run, payment, "RETURN", eid, now),
               Replace(pk, prefix + "PAYMENT#" + pid, new, payment["_v"]),
               Put(pk, prefix + "EVENT#" + eid, self._event_record(event, event_hash, pid, APPLIED, now,
                                                                   effects=["RETURN"])),
               self._delivery(namespace, run, event, APPLIED, "compensating return entry posted", pid)]
        return ops, {"outcome": APPLIED, "event_id": eid, "payment_id": pid, "status": RETURNED, "effects": ["RETURN"]}

    def _plan_park(self, namespace, run, payment, event, event_hash):
        pk, prefix, now = self.pk(namespace), self.prefix(run), iso(self.clock())
        pid, eid = payment["payment_id"], event["event_id"]
        new = strip(payment) | {"pending_return": {"event_id": eid, "return_code": event["return_code"],
                                                   "received_at": now}, "updated_at": now,
                                "history": payment["history"] + [{"at": now, "status": payment["status"], "event_id": eid,
                                                                  "reason": "return received before settlement; parked"}]}
        ops = [self._check_run(namespace, run), Replace(pk, prefix + "PAYMENT#" + pid, new, payment["_v"]),
               Put(pk, prefix + "EVENT#" + eid, self._event_record(event, event_hash, pid, PARKED, now)),
               self._delivery(namespace, run, event, PARKED, "awaiting settlement; will apply atomically with it", pid)]
        return ops, {"outcome": PARKED, "event_id": eid, "payment_id": pid, "status": payment["status"]}

    def _plan_duplicate_effect(self, namespace, run, payment, event, event_hash, reason):
        pk, prefix, now = self.pk(namespace), self.prefix(run), iso(self.clock())
        pid, eid = payment["payment_id"], event["event_id"]
        ops = [self._check_run(namespace, run),
               Put(pk, prefix + "EVENT#" + eid, self._event_record(event, event_hash, pid, DUPLICATE_EFFECT, now,
                                                                   reason=reason)),
               self._delivery(namespace, run, event, DUPLICATE_EFFECT, reason, pid)]
        return ops, {"outcome": DUPLICATE_EFFECT, "event_id": eid, "payment_id": pid, "status": payment["status"],
                     "reason": reason}
