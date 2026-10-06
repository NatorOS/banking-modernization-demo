"""Payment-provider interface and the deterministic simulated provider.

The simulator is the only provider used by the mandatory demo. It is not a model of Column,
Thought Machine, Finxact or any other real core/payment provider.
"""
import hashlib
import json
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Optional, Protocol

from .store import ConditionFailed, Increment, Put, Replace


@dataclass(frozen=True)
class ProviderSubmission:
    namespace: str
    idempotency_key: str  # scoped to namespace, run and payment
    client_reference: str
    amount_cents: int
    currency: str
    beneficiary: str


@dataclass(frozen=True)
class ProviderPayment:
    provider_payment_id: str
    idempotency_key: str
    status: str  # ACCEPTED | SETTLED | RETURNED
    amount_cents: int
    currency: str


class ProviderTimeout(Exception):
    """The provider may or may not have accepted the submission (ambiguous)."""


class ProviderUnavailable(Exception):
    """Transport failure; outcome unknown, treated like a timeout."""


class ProviderRejected(Exception):
    """Definitive rejection: the provider did not and will not create the payment."""

    def __init__(self, reason):
        super().__init__(reason)
        self.reason = reason


class SimulatedCrash(BaseException):
    """Test-only: simulates the worker process dying mid-operation."""


class PaymentProvider(Protocol):
    name: str
    mode: str

    def submit(self, submission: ProviderSubmission) -> ProviderPayment: ...

    def lookup(self, namespace: str, idempotency_key: str) -> Optional[ProviderPayment]: ...


def _iso(dt):
    return dt.astimezone(timezone.utc).isoformat(timespec="microseconds").replace("+00:00", "Z")


class SimulatedProvider:
    """Deterministic, durable provider simulator with provider-side idempotency.

    State lives under ``SIM#`` keys in the namespace partition so it survives Lambda cold
    starts. It is deliberately *not* run-scoped: resetting the subledger does not erase the
    provider's history, just as a real reset could not erase external payment history.
    """

    name = "simulator"
    mode = "deterministic-simulator"
    BLOCKED_MARKER = "REJECT"

    def __init__(self, store, signer, clock=None):
        self.store = store
        self.signer = signer
        self.clock = clock or (lambda: datetime.now(timezone.utc))
        self.submit_calls = 0
        self.lookup_calls = 0
        # Test hooks: called as hook(submission, payment) after acceptance, before responding.
        self.after_accept = []
        self.before_accept = []

    @staticmethod
    def pk(namespace):
        return f"NS#{namespace}"

    @staticmethod
    def provider_payment_id(idempotency_key):
        return "sim_" + hashlib.sha256(idempotency_key.encode()).hexdigest()[:20]

    def _record(self, item):
        return ProviderPayment(item["provider_payment_id"], item["idempotency_key"], item["status"],
                               item["amount_cents"], item["currency"])

    def arm_timeout_after_accept(self, namespace, count=1):
        """Fault injection: the next ``count`` new acceptances respond with a timeout."""
        pk, sk = self.pk(namespace), "SIM#FAULTS"
        current = self.store.get(pk, sk)
        if current is None:
            try:
                self.store.transact([Put(pk, sk, {"timeout_after_accept": count})])
                return
            except ConditionFailed:
                current = self.store.get(pk, sk)
        self.store.transact([Increment(pk, sk, {"timeout_after_accept": count})])

    def _consume_timeout_fault(self, namespace):
        pk, sk = self.pk(namespace), "SIM#FAULTS"
        faults = self.store.get(pk, sk)
        if not faults or faults.get("timeout_after_accept", 0) <= 0:
            return False
        try:
            self.store.transact([Increment(pk, sk, {"timeout_after_accept": -1})])
            return True
        except ConditionFailed:
            return False

    def submit(self, submission):
        self.submit_calls += 1
        for hook in list(self.before_accept):
            hook(submission)
        if self.BLOCKED_MARKER in submission.beneficiary.upper():
            raise ProviderRejected("beneficiary_blocked_by_simulator_rule")
        pk, sk = self.pk(submission.namespace), "SIM#PAYMENT#" + submission.idempotency_key
        item = {
            "provider_payment_id": self.provider_payment_id(submission.idempotency_key),
            "idempotency_key": submission.idempotency_key, "client_reference": submission.client_reference,
            "amount_cents": submission.amount_cents, "currency": submission.currency,
            "beneficiary": submission.beneficiary, "status": "ACCEPTED", "accepted_at": _iso(self.clock()),
        }
        created = True
        try:
            self.store.transact([Put(pk, sk, item)])
        except ConditionFailed:
            created = False
            item = self.store.get(pk, sk)
            if (item["amount_cents"], item["currency"], item["beneficiary"]) != (
                    submission.amount_cents, submission.currency, submission.beneficiary):
                raise ProviderRejected("provider_idempotency_key_reused_with_different_payload") from None
        payment = self._record(item)
        for hook in list(self.after_accept):
            hook(submission, payment)
        if created and self._consume_timeout_fault(submission.namespace):
            raise ProviderTimeout("simulated timeout after the provider accepted the submission")
        return payment

    def lookup(self, namespace, idempotency_key):
        self.lookup_calls += 1
        item = self.store.get(self.pk(namespace), "SIM#PAYMENT#" + idempotency_key)
        return self._record(item) if item else None

    def payments(self, namespace):
        return [self._record(i) for i in
                self.store.query(self.pk(namespace), "SIM#PAYMENT#")]

    # Event emission -------------------------------------------------------------------------

    def _event(self, namespace, idempotency_key, kind, variant=0, return_code=None):
        pk = self.pk(namespace)
        item = self.store.get(pk, "SIM#PAYMENT#" + idempotency_key)
        if item is None:
            raise LookupError("simulator has no payment for that reference")
        suffix = f"_r{variant}" if variant else ""
        event = {
            "event_id": f"evt_{item['provider_payment_id']}_{kind}{suffix}",
            "type": f"payment.{kind}", "provider": self.name,
            "provider_payment_id": item["provider_payment_id"], "client_reference": item["client_reference"],
            "amount_cents": item["amount_cents"], "currency": item["currency"],
            "occurred_at": _iso(self.clock()),
        }
        if kind == "returned":
            event["return_code"] = return_code or "R01"
        existing = self.store.get(pk, "SIM#EVENT#" + event["event_id"])
        if existing:
            return existing["payload"]
        status = {"settled": "SETTLED", "returned": "RETURNED"}[kind]
        try:
            ops = [Put(pk, "SIM#EVENT#" + event["event_id"], {"payload": event})]
            if item["status"] != "RETURNED":
                ops.append(Replace(pk, item["sk"], {k: v for k, v in item.items() if k not in ("pk", "sk", "_v")}
                                   | {"status": status}, item["_v"]))
            self.store.transact(ops)
        except ConditionFailed:
            existing = self.store.get(pk, "SIM#EVENT#" + event["event_id"])
            if existing:
                return existing["payload"]
            raise
        return event

    def settlement_event(self, namespace, idempotency_key, variant=0):
        return self._event(namespace, idempotency_key, "settled", variant)

    def return_event(self, namespace, idempotency_key, return_code="R01", variant=0):
        return self._event(namespace, idempotency_key, "returned", variant, return_code)

    def emitted_event(self, namespace, event_id):
        item = self.store.get(self.pk(namespace), "SIM#EVENT#" + event_id)
        return item["payload"] if item else None

    def signed_delivery(self, event):
        """Serialize and sign an event exactly as a webhook delivery (fresh timestamp per delivery)."""
        body = json.dumps(event, sort_keys=True, separators=(",", ":")).encode()
        return body, self.signer.sign(body)
