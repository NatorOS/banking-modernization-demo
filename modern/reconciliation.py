"""Reconciliation: ledger invariants, provider-vs-subledger state and expected-vs-actual results."""
import json
from datetime import datetime, timezone
from pathlib import Path

from .service import PARKED, PENDING, RETURNED, SETTLED, STATUSES, SUBMITTED, REJECTED, iso

ROOT = Path(__file__).resolve().parents[1]
EXPECTED_FILE = ROOT / "fixtures" / "modern_expected.json"
STAGES = ("after_settlement", "after_return")
EFFECTS_BY_STATUS = {PENDING: set(), SUBMITTED: set(), REJECTED: set(), SETTLED: {"SETTLEMENT"},
                     RETURNED: {"SETTLEMENT", "RETURN"}}


def load_expected(stage):
    stages = json.loads(EXPECTED_FILE.read_text())["stages"]
    if stage not in stages:
        raise ValueError(f"unknown stage {stage!r}; expected one of {sorted(stages)}")
    spec = stages[stage]
    if "golden_file" in spec:
        return json.loads((ROOT / spec["golden_file"]).read_text())
    return spec["expected"]


def ledger_report(snapshot):
    """Golden-compatible report (same keys as legacy.batch.report) plus subledger extras."""
    payments, journal, balance = snapshot["payments"], snapshot["journal"], snapshot["balance"]
    debit = sum(j["amount_cents"] for j in journal if j["side"] == "DEBIT")
    credit = sum(j["amount_cents"] for j in journal if j["side"] == "CREDIT")
    outflow = sum(j["amount_cents"] * (1 if j["side"] == "DEBIT" else -1)
                  for j in journal if j["account"] == "payment_outflow")
    return {
        "currency": balance["currency"], "cash_cents": balance["cash_cents"],
        "settled_cents": sum(p["amount_cents"] for p in payments if p["status"] == SETTLED),
        "debit_cents": debit, "credit_cents": credit, "payment_count": len(payments),
        "statuses": {p["payment_id"]: p["status"] for p in payments},
        "net_settled_outflow_cents": outflow,
        "returned_cents": sum(p["amount_cents"] for p in payments if p["status"] == RETURNED),
        "reserved_cents": balance["reserved_cents"], "available_cents": balance["available_cents"],
        "opening_cash_cents": balance["opening_cash_cents"],
    }


def _check(name, ok, expected=None, actual=None):
    return {"check": name, "ok": bool(ok), "expected": expected, "actual": actual}


def invariant_checks(snapshot):
    balance, payments, journal = snapshot["balance"], snapshot["payments"], snapshot["journal"]
    cash, reserved, available = balance["cash_cents"], balance["reserved_cents"], balance["available_cents"]
    checks = [
        _check("available = cash - reserved", available == cash - reserved, cash - reserved, available),
        _check("available >= 0", available >= 0, ">= 0", available),
        _check("reserved >= 0", reserved >= 0, ">= 0", reserved),
    ]
    debit = sum(j["amount_cents"] for j in journal if j["side"] == "DEBIT")
    credit = sum(j["amount_cents"] for j in journal if j["side"] == "CREDIT")
    checks.append(_check("journal debits = credits", debit == credit, debit, credit))
    journal_cash = balance["opening_cash_cents"] + sum(
        j["amount_cents"] * (1 if j["side"] == "DEBIT" else -1) for j in journal if j["account"] == "cash")
    checks.append(_check("cash balance = opening + journal cash movements", journal_cash == cash, journal_cash, cash))
    in_flight = sum(p["amount_cents"] for p in payments if p["status"] in (PENDING, SUBMITTED))
    checks.append(_check("reserved = in-flight payment amounts", in_flight == reserved, in_flight, reserved))
    by_payment = {}
    for j in journal:
        by_payment.setdefault(j["payment_id"], []).append(j)
    for p in payments:
        entries = by_payment.pop(p["payment_id"], [])
        effects = {j["effect"] for j in entries}
        expected = EFFECTS_BY_STATUS.get(p["status"])
        checks.append(_check(f"{p['payment_id']} journal effects match status {p['status']}",
                             p["status"] in STATUSES and effects == expected and len(entries) == 2 * len(expected),
                             sorted(expected or []), sorted(effects)))
        for effect in effects:
            lines = [j for j in entries if j["effect"] == effect]
            ok = (len(lines) == 2 and {j["side"] for j in lines} == {"DEBIT", "CREDIT"}
                  and all(j["amount_cents"] == p["amount_cents"] for j in lines))
            checks.append(_check(f"{p['payment_id']} {effect} entry balanced", ok, 2 * p["amount_cents"],
                                 sum(j["amount_cents"] for j in lines)))
    checks.append(_check("no journal lines without a payment", not by_payment, [], sorted(by_payment)))
    return checks


def provider_checks(snapshot, provider):
    """Compare simulator (provider-side) state with the subledger for the active run."""
    if provider is None or not hasattr(provider, "payments"):
        return [], []
    prefix = f"{snapshot['namespace']}:{snapshot['run']}:"
    remote = {p.idempotency_key: p for p in provider.payments(snapshot["namespace"])
              if p.idempotency_key.startswith(prefix)}
    checks, explained = [], []
    for p in snapshot["payments"]:
        r = remote.pop(p["provider_idempotency_key"], None)
        local = p["status"]
        if r is None:
            ok = local in (PENDING, REJECTED)
            if local == PENDING:
                explained.append({"payment_id": p["payment_id"], "explanation":
                                  "submission not accepted by provider yet; run reconcile-pending"})
            checks.append(_check(f"{p['payment_id']} provider record", ok, "none for PENDING/REJECTED", local))
            continue
        expected_local = {"ACCEPTED": (SUBMITTED, PENDING), "SETTLED": (SETTLED, SUBMITTED, PENDING),
                          "RETURNED": (RETURNED, SETTLED, SUBMITTED, PENDING)}[r.status]
        ok = local in expected_local and r.amount_cents == p["amount_cents"]
        if ok and local == PENDING:
            explained.append({"payment_id": p["payment_id"], "explanation":
                              "provider accepted but acceptance not yet recorded (timeout/crash); run reconcile-pending"})
        elif ok and {"ACCEPTED": SUBMITTED, "SETTLED": SETTLED, "RETURNED": RETURNED}[r.status] != local:
            explained.append({"payment_id": p["payment_id"], "explanation":
                              f"provider reports {r.status}; local {local} awaits event delivery"})
        checks.append(_check(f"{p['payment_id']} provider state {r.status} consistent", ok, expected_local, local))
    for key in remote:
        checks.append(_check(f"provider payment {key} has a subledger record", False, "local payment", None))
    return checks, explained


def compare(actual, expected):
    differences = []
    for key, value in expected.items():
        if key == "statuses":
            for pid in sorted(set(value) | set(actual.get("statuses", {}))):
                a, e = actual.get("statuses", {}).get(pid), value.get(pid)
                if a != e:
                    differences.append({"field": f"statuses.{pid}", "expected": e, "actual": a})
        elif actual.get(key) != value:
            differences.append({"field": key, "expected": value, "actual": actual.get(key)})
    return differences


def build_report(snapshot, provider=None, stage=None):
    actual = ledger_report(snapshot)
    expected = load_expected(stage) if stage else None
    differences = compare(actual, expected) if expected else []
    invariants = invariant_checks(snapshot)
    provider_results, explained = provider_checks(snapshot, provider)
    for e in snapshot["events"]:
        if e["outcome"] == PARKED:
            explained.append({"event_id": e["event_id"], "payment_id": e["payment_id"],
                              "explanation": "return parked until its settlement arrives"})
    rejected = [{"event_id": d["event_id"], "reason": d["reason"], "received_at": d["received_at"]}
                for d in snapshot["deliveries"] if d["outcome"] == "REJECTED"]
    failed = [c for c in invariants + provider_results if not c["ok"]]
    unexplained = len(differences) + len(failed)
    return {
        "report": "modern-payment-reconciliation", "generated_at": iso(datetime.now(timezone.utc)),
        "namespace": snapshot["namespace"], "run": snapshot["run"],
        "provider": {"name": getattr(provider, "name", None), "mode": getattr(provider, "mode", None)},
        "stage": stage, "expected": expected, "actual": actual, "differences": differences,
        "invariants": invariants, "provider_reconciliation": provider_results,
        "explained_exceptions": explained, "rejected_events": rejected,
        "unexplained_differences": unexplained, "status": "PASS" if unexplained == 0 else "FAIL",
    }
