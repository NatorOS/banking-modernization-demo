"""Repeatable demo: legacy baseline, modern equivalent, duplicates, return and reconciliation.

Local:  python3 -m modern.demo run
AWS:    python3 -m modern.demo run --remote https://<api-id>.execute-api.us-east-1.amazonaws.com --profile natoros
"""
import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


class Demo:
    def __init__(self, client, namespace, out=sys.stdout):
        self.client = client
        self.ns = namespace
        self.out = out
        self.failures = []

    def say(self, text=""):
        print(text, file=self.out)

    def check(self, label, ok, detail=""):
        self.say(f"  [{'PASS' if ok else 'FAIL'}] {label}" + (f" ({detail})" if detail else ""))
        if not ok:
            self.failures.append(label)
        return ok

    def call(self, method, path, payload=None, headers=None, query=""):
        return self.client.request(method, f"/api/namespaces/{self.ns}{path}" if not path.startswith("/api")
                                   else path, payload, headers, query)

    def action(self, name, payload=None):
        status, body = self.call("POST", f"/demo/{name}", payload or {})
        if status != 200:
            self.check(f"{name} action accepted", False, json.dumps(body)[:300])
            return []
        return body["results"]

    def state(self):
        status, body = self.call("GET", "/state")
        return body

    def ledger(self):
        return self.state()["reconciliation"]["actual"]

    def balances(self, label):
        a = self.ledger()
        self.say(f"  {label}: cash={a['cash_cents']} reserved={a['reserved_cents']} available={a['available_cents']} "
                 f"debits={a['debit_cents']} credits={a['credit_cents']} statuses={a['statuses']}")
        return a

    def run(self):
        self.say("Step 1. Legacy batch baseline (unchanged legacy/batch.py)")
        status, legacy = self.client.request("GET", "/api/legacy/baseline")
        self.check("legacy batch matches fixtures/golden_batch.json", status == 200 and legacy["matches_golden"],
                   f"cash={legacy['result']['cash_cents']}")

        self.say(f"\nStep 2. Reset synthetic namespace {self.ns} (fresh run generation)")
        status, reset = self.call("POST", "/reset", {})
        self.check("reset started a new run", status == 200, f"run={reset.get('run')}")

        self.say("\nStep 3. Submit the three fixture payments with Idempotency-Keys (funds reserved, no journal)")
        results = self.action("submit-fixtures")
        self.check("3 payments SUBMITTED", [r["payment"]["status"] for r in results] == ["SUBMITTED"] * 3)
        a = self.balances("after submission")
        self.check("reserved 18500, available 81500, cash unchanged, journal empty",
                   (a["reserved_cents"], a["available_cents"], a["cash_cents"], a["debit_cents"]) == (18500, 81500, 100000, 0))

        self.say("\nStep 4. Retry the same submissions (client retry)")
        results = self.action("submit-fixtures")
        self.check("all 3 replayed without a new provider submission",
                   all(r["replayed"] and r["outcome"] == "existing" for r in results))
        status, conflict = self.call("POST", "/payments", {"payment_id": "PAY-001", "beneficiary": "Synthetic Supplier A",
                                                           "amount_cents": 99999, "currency": "USD"},
                                     {"Idempotency-Key": "fixture-PAY-001"})
        self.check("same key, different payload -> 409 conflict", status == 409, conflict["error"]["code"])

        self.say("\nStep 5. Provider settlement events (signed webhooks)")
        results = self.action("settle-all")
        self.check("3 settlements APPLIED", [r.get("outcome") for r in results] == ["APPLIED"] * 3)
        status, report = self.call("GET", "/reconciliation", query="stage=after_settlement")
        self.check("modern outcome equals the legacy golden result", report["status"] == "PASS",
                   f"differences={report['differences']}")
        self.balances("after settlement")

        self.say("\nStep 6. Duplicate settlement deliveries")
        r = self.action("replay-settlement")[0]
        self.check("same event_id replayed -> DUPLICATE_EVENT", r.get("outcome") == "DUPLICATE_EVENT")
        r = self.action("duplicate-settlement")[0]
        self.check("new event_id for the same settlement -> DUPLICATE_EFFECT", r.get("outcome") == "DUPLICATE_EFFECT")
        a = self.balances("after duplicates")
        self.check("no double posting", (a["cash_cents"], a["debit_cents"]) == (81500, 18500))

        self.say("\nStep 7. Return PAY-002 (R01 insufficient funds) after settlement")
        r = self.action("return-pay-002")[0]
        self.check("return APPLIED with one compensating entry", r.get("outcome") == "APPLIED")
        a = self.balances("after return")
        self.check("cash 85000, net settled outflow 15000, debits/credits 22000",
                   (a["cash_cents"], a["net_settled_outflow_cents"], a["debit_cents"], a["credit_cents"])
                   == (85000, 15000, 22000, 22000))

        self.say("\nStep 8. Replay the return")
        r = self.action("replay-return")[0]
        self.check("same return event_id -> DUPLICATE_EVENT", r.get("outcome") == "DUPLICATE_EVENT")
        r = self.action("provider-event", {"payment_id": "PAY-002", "type": "returned", "variant": 2})[0]
        self.check("new event_id for the same return -> DUPLICATE_EFFECT", r.get("outcome") == "DUPLICATE_EFFECT")

        self.say("\nStep 9. Reconciliation against expected results (after_return)")
        status, report = self.call("GET", "/reconciliation", query="stage=after_return")
        self.check("reconciliation PASS with zero unexplained differences",
                   report["status"] == "PASS" and report["unexplained_differences"] == 0,
                   f"invariants={sum(c['ok'] for c in report['invariants'])}/{len(report['invariants'])}")
        return report

    def edge_cases(self):
        base = self.ns
        self.ns = f"{base}-edge"
        self.say(f"Edge cases in namespace {self.ns}")
        self.call("POST", "/reset", {})
        pay = lambda pid, amount=1000, **kw: dict({"payment_id": pid, "beneficiary": "Synthetic Edge Co",
                                                   "amount_cents": amount, "currency": "USD"}, **kw)

        self.say("\nA. Invalid input fails without ledger changes")
        before = self.ledger()
        for label, body in [("amount 0", pay("PAY-900", 0)), ("amount as string", pay("PAY-900", "100")),
                            ("fractional amount", pay("PAY-900", 10.5)), ("currency EUR", pay("PAY-900", currency="EUR")),
                            ("bad payment reference", pay("pay 900"))]:
            status, body = self.call("POST", "/payments", body, {"Idempotency-Key": "edge-invalid-0001"})
            self.check(f"{label} -> 400", status == 400, body["error"]["code"])
        status, body = self.call("POST", "/payments", pay("PAY-901", 500000), {"Idempotency-Key": "edge-nsf-0001"})
        self.check("insufficient cash -> 422", status == 422, body["error"]["code"])
        self.check("ledger unchanged after rejected inputs", self.ledger() == before)

        self.say("\nB. Provider timeout after it accepted the submission")
        self.action("arm-timeout", {"count": 1})
        status, first = self.call("POST", "/payments", pay("PAY-902", 2000), {"Idempotency-Key": "edge-timeout-0001"})
        self.check("first attempt -> 202 outcome unknown, still PENDING_SUBMISSION, funds reserved",
                   status == 202 and first["payment"]["status"] == "PENDING_SUBMISSION", first["outcome"])
        status, retry = self.call("POST", "/payments", pay("PAY-902", 2000), {"Idempotency-Key": "edge-timeout-0001"})
        self.check("retry -> SUBMITTED with the same provider payment (provider idempotency)",
                   retry["payment"]["status"] == "SUBMITTED", retry["payment"]["provider_payment_id"])

        self.say("\nC. Return delivered before settlement")
        self.call("POST", "/payments", pay("PAY-903", 3000), {"Idempotency-Key": "edge-ooo-00001"})
        r = self.action("provider-event", {"payment_id": "PAY-903", "type": "returned"})[0]
        self.check("early return -> PARKED (no posting)", r.get("outcome") == "PARKED")
        r = self.action("provider-event", {"payment_id": "PAY-903", "type": "settled"})[0]
        self.check("settlement -> APPLIED together with the parked return", r.get("status") == "RETURNED",
                   f"applied_parked_event_ids={r.get('applied_parked_event_ids')}")

        self.say("\nD. Invalid provider events")
        state = self.state()["snapshot"]
        p = next(x for x in state["payments"] if x["payment_id"] == "PAY-902")
        good = {"event_id": "evt_edge_bad_amount", "type": "payment.settled", "provider": "simulator",
                "provider_payment_id": p["provider_payment_id"], "client_reference": p["provider_idempotency_key"],
                "amount_cents": 1, "currency": "USD", "occurred_at": "2026-10-06T00:00:00Z"}
        r = self.action("deliver-event", {"event": good})[0]
        self.check("wrong amount -> 422 rejected", r.get("http_status") == 422, r.get("error", {}).get("message"))
        r = self.action("deliver-event", {"event": dict(good, event_id="evt_edge_unknown",
                                                         client_reference=f"{self.ns}:1:PAY-999", amount_cents=2000)})[0]
        self.check("unknown payment -> 422 rejected", r.get("http_status") == 422)

        self.say("\nE. Reset isolation: events from a previous run cannot touch the new run")
        r = self.action("provider-event", {"payment_id": "PAY-902", "type": "settled"})[0]
        old_event = r["event_id"]
        self.call("POST", "/reset", {})
        r = self.action("redeliver", {"event_id": old_event})[0]
        self.check("old-run settlement redelivered after reset -> rejected as stale_run",
                   r.get("http_status") == 422 and "stale_run" in r.get("error", {}).get("message", ""))
        a = self.ledger()
        self.check("new run starts clean", (a["cash_cents"], a["payment_count"], a["debit_cents"]) == (100000, 0, 0))
        status, report = self.call("GET", "/reconciliation")
        self.check("edge namespace reconciliation PASS", report["status"] == "PASS")
        self.ns = base


def make_client(args):
    if args.remote:
        from .aws_client import SignedApiClient
        return SignedApiClient(args.remote, args.profile, args.region)
    from .aws_client import LocalClient
    from .config import build_app
    return LocalClient(build_app(sqlite_path=args.db))


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("command", choices=["run", "edge-cases", "reset", "reconcile-pending", "report", "purge"])
    parser.add_argument("--namespace", default=None)
    parser.add_argument("--db", default="data/modern.sqlite", help="local SQLite path")
    parser.add_argument("--remote", help="deployed HTTP API base URL (requests are SigV4-signed)")
    parser.add_argument("--profile", help="AWS profile for --remote")
    parser.add_argument("--region", default="us-east-1")
    parser.add_argument("--report", default="output/reconciliation.json")
    args = parser.parse_args(argv)
    namespace = args.namespace or ("demo-aws" if args.remote else "demo-local")
    demo = Demo(make_client(args), namespace)
    if args.command == "run":
        report = demo.run()
        Path(args.report).parent.mkdir(parents=True, exist_ok=True)
        Path(args.report).write_text(json.dumps(report, indent=2) + "\n")
        demo.say(f"\nReport written to {args.report}")
    elif args.command == "edge-cases":
        demo.edge_cases()
    elif args.command == "reset":
        print(json.dumps(demo.call("POST", "/reset", {})[1], indent=2))
    elif args.command == "purge":
        print(json.dumps(demo.call("POST", "/purge", {"confirm": namespace})[1], indent=2))
    elif args.command == "reconcile-pending":
        print(json.dumps(demo.call("POST", "/reconcile-pending", {})[1], indent=2))
    elif args.command == "report":
        print(json.dumps(demo.call("GET", "/reconciliation")[1], indent=2))
    if demo.failures:
        print(f"\n{len(demo.failures)} check(s) FAILED", file=sys.stderr)
        return 1
    if args.command in ("run", "edge-cases"):
        demo.say("\nAll checks passed.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
