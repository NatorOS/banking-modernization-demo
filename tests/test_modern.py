"""Behavioral suite for the modern payment integration.

The same ``BehaviorSuite`` runs against SqliteStore (stdlib only) and DynamoStore (via moto, if
installed). Concurrency, crash, late-response and reset races use injected hooks and a
controllable clock, so the outcomes are deterministic.
"""
import json
import os
import sqlite3
import tempfile
import threading
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest import mock

from modern.api import App, fixture, fixture_key
from modern.provider import ProviderRejected, SimulatedCrash, SimulatedProvider
from modern.reconciliation import build_report
from modern.service import (REJECTED_EVENT, EventConflict, IdempotencyConflict, InsufficientFunds, NotFound,
                            PaymentConflict, PaymentService, RejectedEvent, ServiceBusy, Unauthorized,
                            ValidationError)
from modern.store import ConditionFailed, Contention, Put, SqliteStore
from modern.webhook import WebhookSigner

try:
    import boto3
    from moto import mock_aws
except ImportError:  # the original CI job runs without third-party packages
    boto3 = mock_aws = None

ROOT = Path(__file__).resolve().parents[1]
GOLDEN = json.loads((ROOT / "fixtures" / "golden_batch.json").read_text())
KEY = "k" * 64
NS = "demo-test"


class Clock:
    def __init__(self):
        self.now = datetime(2026, 10, 6, 12, 0, tzinfo=timezone.utc)

    def __call__(self):
        return self.now

    def advance(self, seconds):
        self.now += timedelta(seconds=seconds)


class BehaviorSuite:
    def make_store(self):
        raise NotImplementedError

    def reopen_store(self):
        raise NotImplementedError

    def setUp(self):
        self.clock = Clock()
        self.store = self.make_store()
        self.build(self.store)
        self.service.reset(NS)

    def build(self, store):
        self.signer = WebhookSigner(KEY, clock=self.clock)
        self.provider = SimulatedProvider(store, self.signer, clock=self.clock)
        self.service = PaymentService(store, self.provider, verifier=self.signer, clock=self.clock,
                                      sleep=lambda s: None)
        self.app = App(self.service, self.provider)

    # Helpers ---------------------------------------------------------------------------------

    def request(self, pid="PAY-001", amount=None, beneficiary=None, currency="USD"):
        fx = {p["id"]: p for p in fixture()["payments"]}.get(pid)
        return {"payment_id": pid, "beneficiary": beneficiary or (fx["beneficiary"] if fx else "Synthetic Co"),
                "amount_cents": amount if amount is not None else (fx["amount_cents"] if fx else 1000),
                "currency": currency}

    def submit(self, pid="PAY-001", key=None, ns=NS, **kw):
        return self.service.submit_payment(ns, key or fixture_key(pid), self.request(pid, **kw))

    def submit_fixtures(self):
        return [self.submit(p["id"]) for p in fixture()["payments"]]

    def payment(self, pid, ns=NS):
        return self.service.get_payment(ns, pid)

    def deliver(self, event, ns=NS):
        body, headers = self.provider.signed_delivery(event)
        return self.service.ingest_webhook(ns, body, headers)

    def settle(self, pid, variant=0, ns=NS):
        return self.deliver(self.provider.settlement_event(ns, self.payment(pid, ns)["provider_idempotency_key"],
                                                           variant), ns)

    def give_back(self, pid, variant=0, ns=NS):
        return self.deliver(self.provider.return_event(ns, self.payment(pid, ns)["provider_idempotency_key"],
                                                       "R01", variant), ns)

    def balance(self, ns=NS):
        return self.service.snapshot(ns)["balance"]

    def report(self, stage=None, ns=NS):
        return build_report(self.service.snapshot(ns), self.provider, stage)

    def assert_invariants(self, ns=NS):
        b = self.balance(ns)
        self.assertEqual(b["available_cents"], b["cash_cents"] - b["reserved_cents"])
        self.assertGreaterEqual(b["available_cents"], 0)
        self.assertGreaterEqual(b["reserved_cents"], 0)
        report = self.report(ns=ns)
        failed = [c for c in report["invariants"] + report["provider_reconciliation"] if not c["ok"]]
        self.assertEqual(failed, [])
        return b

    # Golden and returned outcomes ---------------------------------------------------------------

    def test_settlement_matches_legacy_golden(self):
        self.submit_fixtures()
        b = self.assert_invariants()
        self.assertEqual((b["cash_cents"], b["reserved_cents"], b["available_cents"]), (100000, 18500, 81500))
        self.assertEqual(self.service.snapshot(NS)["journal"], [])
        for p in fixture()["payments"]:
            self.settle(p["id"])
        report = self.report("after_settlement")
        self.assertEqual(report["status"], "PASS", report["differences"])
        actual = report["actual"]
        for field in ("cash_cents", "settled_cents", "debit_cents", "credit_cents", "payment_count", "statuses"):
            self.assertEqual(actual[field], GOLDEN[field], field)
        self.assertEqual(actual["reserved_cents"], 0)
        self.assert_invariants()

    def test_return_produces_exact_expected_values(self):
        self.submit_fixtures()
        for p in fixture()["payments"]:
            self.settle(p["id"])
        result = self.give_back("PAY-002")
        self.assertEqual(result["outcome"], "APPLIED")
        actual = self.report("after_return")["actual"]
        self.assertEqual(actual["cash_cents"], 85000)
        self.assertEqual(actual["net_settled_outflow_cents"], 15000)
        self.assertEqual((actual["debit_cents"], actual["credit_cents"]), (22000, 22000))
        self.assertEqual((actual["reserved_cents"], actual["available_cents"]), (0, 85000))
        self.assertEqual(actual["statuses"], {"PAY-001": "SETTLED", "PAY-002": "RETURNED", "PAY-003": "SETTLED"})
        self.assertEqual(self.report("after_return")["status"], "PASS")
        returned = [j for j in self.service.snapshot(NS)["journal"] if j["effect"] == "RETURN"]
        self.assertEqual({(j["side"], j["account"]) for j in returned},
                         {("DEBIT", "cash"), ("CREDIT", "payment_outflow")})
        self.assert_invariants()

    # Submission idempotency ---------------------------------------------------------------------

    def test_same_key_same_payload_replays_without_new_provider_call(self):
        first = self.submit("PAY-001")
        calls = self.provider.submit_calls
        again = self.submit("PAY-001")
        self.assertTrue(again["replayed"])
        self.assertEqual(again["payment"]["provider_payment_id"], first["payment"]["provider_payment_id"])
        self.assertEqual(self.provider.submit_calls, calls)
        self.assertEqual(self.balance()["reserved_cents"], 12500)

    def test_same_key_different_payload_is_rejected(self):
        self.submit("PAY-001")
        before = self.service.snapshot(NS)
        with self.assertRaises(IdempotencyConflict):
            self.submit("PAY-001", amount=4999)
        with self.assertRaises(PaymentConflict):
            self.submit("PAY-001", key="other-key-0001")
        self.assertEqual(self.service.snapshot(NS)["balance"], before["balance"])
        record = self.service.snapshot(NS)["idempotency_records"][0]
        self.assertEqual(json.loads(record["canonical_payload"])["amount_cents"], 12500)

    def test_state_survives_process_restart(self):
        self.submit_fixtures()
        self.settle("PAY-001")
        before = self.service.snapshot(NS)
        self.build(self.reopen_store())
        self.assertEqual(self.service.snapshot(NS), before)
        replay = self.submit("PAY-002")
        self.assertTrue(replay["replayed"])
        self.assertEqual(self.provider.submit_calls, 0)
        self.assertEqual(self.settle("PAY-001")["outcome"], "DUPLICATE_EVENT")

    def test_concurrent_same_key_creates_one_payment_and_one_reservation(self):
        results, errors = [], []

        def worker():
            try:
                results.append(self.submit("PAY-001"))
            except Exception as exc:  # noqa: BLE001 - collected for assertion
                errors.append(exc)

        threads = [threading.Thread(target=worker) for _ in range(8)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        self.assertEqual(errors, [])
        self.assertEqual(len(self.service.snapshot(NS)["payments"]), 1)
        self.assertEqual(len(self.provider.payments(NS)), 1)
        self.assertEqual(self.balance()["reserved_cents"], 12500)
        self.assert_invariants()

    def test_concurrent_attempts_to_spend_the_same_funds(self):
        self.service.reset(NS, opening_cash_cents=1000)
        outcomes, lock = [], threading.Lock()

        def worker(i):
            try:
                self.submit(f"PAY-{100 + i}", key=f"spend-key-{i:04d}", amount=400)
                outcome = "ok"
            except InsufficientFunds:
                outcome = "nsf"
            with lock:
                outcomes.append(outcome)

        threads = [threading.Thread(target=worker, args=(i,)) for i in range(8)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        self.assertEqual(sorted(outcomes), ["nsf"] * 6 + ["ok"] * 2)
        b = self.assert_invariants()
        self.assertEqual((b["reserved_cents"], b["available_cents"]), (800, 200))

    # Provider events ---------------------------------------------------------------------------

    def test_duplicate_settlement_by_event_id_and_by_effect(self):
        self.submit("PAY-001")
        self.assertEqual(self.settle("PAY-001")["outcome"], "APPLIED")
        before = self.balance()
        self.assertEqual(self.settle("PAY-001")["outcome"], "DUPLICATE_EVENT")
        self.assertEqual(self.settle("PAY-001", variant=2)["outcome"], "DUPLICATE_EFFECT")
        self.assertEqual(self.balance(), before)
        self.assertEqual(len(self.service.snapshot(NS)["journal"]), 2)
        outcomes = [d["outcome"] for d in self.service.snapshot(NS)["deliveries"]]
        self.assertEqual(outcomes, ["APPLIED", "DUPLICATE_EVENT", "DUPLICATE_EFFECT"])

    def test_event_id_reused_with_different_content_is_detected(self):
        self.submit("PAY-001")
        event = self.provider.settlement_event(NS, self.payment("PAY-001")["provider_idempotency_key"])
        self.deliver(event)
        before = self.service.snapshot(NS)["balance"]
        with self.assertRaises(EventConflict):
            self.deliver(dict(event, occurred_at="2030-01-01T00:00:00Z"))
        self.assertEqual(self.balance(), before)
        self.assertEqual(self.service.snapshot(NS)["deliveries"][-1]["outcome"], "REJECTED")

    def test_duplicate_return(self):
        self.submit("PAY-002")
        self.settle("PAY-002")
        self.assertEqual(self.give_back("PAY-002")["outcome"], "APPLIED")
        before = self.balance()
        self.assertEqual(self.give_back("PAY-002")["outcome"], "DUPLICATE_EVENT")
        self.assertEqual(self.give_back("PAY-002", variant=2)["outcome"], "DUPLICATE_EFFECT")
        self.assertEqual(self.balance(), before)
        self.assertEqual(len(self.service.snapshot(NS)["journal"]), 4)
        self.assert_invariants()

    def test_return_before_settlement_matches_in_order_outcome_with_evidence(self):
        self.submit("PAY-002")
        parked = self.give_back("PAY-002")
        self.assertEqual(parked["outcome"], "PARKED")
        self.assertEqual(self.service.snapshot(NS)["journal"], [])
        self.assertEqual(self.give_back("PAY-002", variant=2)["outcome"], "DUPLICATE_EFFECT")
        transactions = []
        self.store.before_transact.append(transactions.append)
        settled = self.settle("PAY-002")
        self.assertEqual(settled["status"], "RETURNED")
        self.assertEqual(settled["applied_parked_event_ids"], [parked["event_id"]])
        balance_ops = [op for op in transactions[-1] if op.sk.endswith("#BALANCE")]
        self.assertEqual(len(balance_ops), 1, "settlement + parked return must be one BALANCE update")
        snap = self.service.snapshot(NS)
        events = {e["event_id"]: e for e in snap["events"]}
        self.assertEqual(events[parked["event_id"]]["outcome"], "APPLIED")
        self.assertEqual(events[parked["event_id"]]["resolved_by_event_id"], settled["event_id"])
        self.assertTrue(all(j["resolved_with_settlement_event_id"] == settled["event_id"]
                            for j in snap["journal"] if j["effect"] == "RETURN"))
        b = self.assert_invariants()
        self.assertEqual((b["cash_cents"], b["reserved_cents"], b["available_cents"]), (100000, 0, 100000))
        self.assertEqual(self.report()["actual"]["debit_cents"], 7000)

    def test_invalid_events_are_rejected_before_duplicate_classification(self):
        self.submit("PAY-001")
        good = self.provider.settlement_event(NS, self.payment("PAY-001")["provider_idempotency_key"])
        self.deliver(good)
        before = self.service.snapshot(NS)
        bad = [dict(good, event_id="evt_bad_amount", amount_cents=4999),
               dict(good, event_id="evt_bad_currency", currency="EUR"),
               dict(good, event_id="evt_bad_ref", client_reference=f"{NS}:1:PAY-404"),
               dict(good, event_id="evt_bad_provider_id", provider_payment_id="sim_other_payment"),
               dict(good, event_id="evt_bad_type", type="payment.reversed"),
               {k: v for k, v in good.items() if k != "amount_cents"} | {"event_id": "evt_missing_amount"}]
        for event in bad:
            with self.assertRaises(RejectedEvent, msg=event["event_id"]):
                self.deliver(event)
        after = self.service.snapshot(NS)
        self.assertEqual((after["balance"], after["journal"], after["payments"]),
                         (before["balance"], before["journal"], before["payments"]))
        self.assertEqual([d["outcome"] for d in after["deliveries"]][-len(bad):], ["REJECTED"] * len(bad))

    def test_webhook_signature_is_required(self):
        self.submit("PAY-001")
        body, headers = self.provider.signed_delivery(
            self.provider.settlement_event(NS, self.payment("PAY-001")["provider_idempotency_key"]))
        for tampered_body, tampered_headers in [(body.replace(b"payment.settled", b"payment.returned"), headers), (body, {}),
                                                (body, dict(headers, **{"X-Provider-Signature": "v1=00"}))]:
            with self.assertRaises(Unauthorized):
                self.service.ingest_webhook(NS, tampered_body, tampered_headers)
        self.clock.advance(301)
        with self.assertRaises(Unauthorized):
            self.service.ingest_webhook(NS, body, headers)
        self.assertEqual(self.payment("PAY-001")["status"], "SUBMITTED")

    # Validation and atomicity ------------------------------------------------------------------

    def test_invalid_submissions_change_nothing(self):
        before = self.service.snapshot(NS)
        cases = [dict(amount=0), dict(amount=-1), dict(amount="100"), dict(amount=10.5), dict(amount=True),
                 dict(currency="EUR"), dict(currency="usd")]
        for kw in cases:
            with self.assertRaises(ValidationError, msg=kw):
                self.submit("PAY-001", **kw)
        with self.assertRaises(ValidationError):
            self.service.submit_payment(NS, "bad key", self.request())
        with self.assertRaises(ValidationError):
            self.service.submit_payment(NS, "valid-key-0001", self.request("pay 1"))
        self.assertEqual(self.service.snapshot(NS), before)

    def test_insufficient_cash_rolls_back_atomically(self):
        self.submit("PAY-001")
        before = self.service.snapshot(NS)
        with self.assertRaises(InsufficientFunds):
            self.submit("PAY-900", key="nsf-key-0001", amount=95001)
        after = self.service.snapshot(NS)
        self.assertEqual(after, before)
        self.assertIsNone(self.payment("PAY-900"))

    def test_provider_rejection_releases_reservation(self):
        result = self.submit("PAY-901", key="reject-key-01", beneficiary="REJECT Synthetic Co", amount=700)
        self.assertEqual(result["payment"]["status"], "REJECTED")
        b = self.assert_invariants()
        self.assertEqual((b["reserved_cents"], b["available_cents"]), (0, 100000))

    # Timeouts, crashes, leases and late responses ------------------------------------------------

    def test_timeout_after_acceptance_stays_pending_then_recovers(self):
        self.provider.arm_timeout_after_accept(NS)
        first = self.submit("PAY-001")
        self.assertEqual(first["outcome"], "submission_outcome_unknown")
        self.assertEqual(first["payment"]["status"], "PENDING_SUBMISSION")
        self.assertEqual(self.balance()["reserved_cents"], 12500)
        self.assertEqual(len(self.provider.payments(NS)), 1)
        retry = self.submit("PAY-001")
        self.assertEqual(retry["payment"]["status"], "SUBMITTED")
        self.assertEqual(len(self.provider.payments(NS)), 1)
        self.assertEqual(retry["payment"]["provider_payment_id"], self.provider.payments(NS)[0].provider_payment_id)
        self.assert_invariants()

    def test_timeout_recovered_by_reconcile_pending_uses_lookup(self):
        self.provider.arm_timeout_after_accept(NS)
        self.submit("PAY-001")
        calls = self.provider.submit_calls
        result = self.service.reconcile_pending(NS)["results"][0]
        self.assertEqual(result["payment"]["status"], "SUBMITTED")
        self.assertEqual(self.provider.submit_calls, calls, "reconcile looks up before resubmitting")

    def test_crash_mid_submission_is_recovered_after_lease_expiry(self):
        def crash(submission, payment):
            raise SimulatedCrash()

        self.provider.after_accept.append(crash)
        with self.assertRaises(SimulatedCrash):
            self.submit("PAY-001")
        self.provider.after_accept.clear()
        stuck = self.payment("PAY-001")
        self.assertEqual(stuck["status"], "PENDING_SUBMISSION")
        self.assertTrue(stuck["lease_owner"])
        self.assertEqual(self.balance()["reserved_cents"], 12500)
        self.assertEqual(self.service.reconcile_pending(NS)["results"][0]["outcome"], "submission_in_progress")
        self.clock.advance(PaymentService.LEASE_SECONDS + 1)
        calls = self.provider.submit_calls
        recovered = self.service.reconcile_pending(NS)["results"][0]
        self.assertEqual(recovered["payment"]["status"], "SUBMITTED")
        self.assertEqual(self.provider.submit_calls, calls)
        self.assertEqual(len(self.provider.payments(NS)), 1)
        self.assert_invariants()

    def test_stale_worker_cannot_overwrite_newer_worker(self):
        fired = []

        def takeover(submission):
            if fired:
                return
            fired.append(True)
            self.clock.advance(PaymentService.LEASE_SECONDS + 1)
            worker_b = self.service.reconcile_pending(NS)["results"][0]
            self.assertEqual(worker_b["payment"]["status"], "SUBMITTED")
            raise ProviderRejected("late rejection seen by the stale worker")

        self.provider.before_accept.append(takeover)
        stale = self.submit("PAY-001")
        self.assertEqual(stale["outcome"], "late_provider_response_ignored")
        self.assertEqual(self.payment("PAY-001")["status"], "SUBMITTED")
        self.assertEqual(self.balance()["reserved_cents"], 12500, "stale rejection must not release funds")
        self.assert_invariants()

    def test_late_acceptance_after_settlement_does_not_downgrade(self):
        def settlement_wins(submission, payment):
            self.provider.after_accept.clear()
            self.assertEqual(self.settle("PAY-001")["status"], "SETTLED")

        self.provider.after_accept.append(settlement_wins)
        late = self.submit("PAY-001")
        self.assertEqual(late["outcome"], "late_provider_response_ignored")
        p = self.payment("PAY-001")
        self.assertEqual(p["status"], "SETTLED")
        self.assertIsNone(p["lease_owner"])
        b = self.assert_invariants()
        self.assertEqual((b["cash_cents"], b["reserved_cents"]), (87500, 0))

    def test_late_rejection_after_return_does_not_release_funds_again(self):
        def settle_and_return_then_reject(submission, payment):
            self.provider.after_accept.clear()
            self.settle("PAY-001")
            self.give_back("PAY-001")
            raise ProviderRejected("late rejection after return")

        self.provider.after_accept.append(settle_and_return_then_reject)
        late = self.submit("PAY-001")
        self.assertEqual(late["outcome"], "late_provider_response_ignored")
        self.assertEqual(self.payment("PAY-001")["status"], "RETURNED")
        b = self.assert_invariants()
        self.assertEqual((b["cash_cents"], b["reserved_cents"], b["available_cents"]), (100000, 0, 100000))

    # Reset isolation -------------------------------------------------------------------------

    def test_reset_isolates_old_events_and_in_flight_responses(self):
        self.service.reset("demo-other")
        self.service.submit_payment("demo-other", "other-ns-key-1", self.request("PAY-001"))
        other_before = self.service.snapshot("demo-other")
        self.submit("PAY-001")
        old_event = self.provider.settlement_event(NS, self.payment("PAY-001")["provider_idempotency_key"])

        def reset_mid_flight(submission, payment):
            self.provider.after_accept.clear()
            self.service.reset(NS)

        self.provider.after_accept.append(reset_mid_flight)
        in_flight = self.submit("PAY-002")
        self.assertEqual(in_flight["outcome"], "late_provider_response_ignored")
        snap = self.service.snapshot(NS)
        self.assertEqual(snap["run"], 2)
        self.assertEqual((snap["payments"], snap["journal"]), ([], []))
        reused = self.submit("PAY-001")
        self.assertEqual(reused["payment"]["status"], "SUBMITTED")
        self.assertEqual(reused["payment"]["provider_idempotency_key"], f"{NS}:2:PAY-001")
        with self.assertRaises(RejectedEvent) as ctx:
            self.deliver(old_event)
        self.assertIn("stale_run", str(ctx.exception))
        self.assertEqual(self.payment("PAY-001")["status"], "SUBMITTED")
        self.assertEqual(self.balance()["cash_cents"], 100000)
        self.assertEqual(self.service.snapshot("demo-other"), other_before)
        self.assert_invariants()

    def run_numbers(self, ns=NS):
        return {self.service.item_run(i) for i in self.store.query(f"NS#{ns}", "RUN#")}

    def test_overlapping_reset_cleanup_keeps_newer_run(self):
        self.submit_fixtures()
        real_delete = self.store.delete_prefix
        state = {"armed": True}

        def delete_prefix(pk, prefix, **kwargs):
            # Reset B starts, commits and cleans up after A committed but before A's cleanup.
            if state.pop("armed", False):
                state["b"] = self.service.reset(NS)
            return real_delete(pk, prefix, **kwargs)

        with mock.patch.object(self.store, "delete_prefix", side_effect=delete_prefix):
            a = self.service.reset(NS)
        self.assertEqual((a["run"], state["b"]["run"]), (2, 3))
        snap = self.service.snapshot(NS)
        self.assertEqual(snap["run"], 3)
        self.assertEqual(self.run_numbers(), {3})
        b = self.assert_invariants()
        self.assertEqual((b["cash_cents"], b["reserved_cents"], b["available_cents"]), (100000, 0, 100000))
        self.submit("PAY-001")
        self.assertEqual(self.settle("PAY-001")["outcome"], "APPLIED")
        self.assert_invariants()

    def test_concurrent_resets_leave_a_usable_active_run(self):
        errors, barrier = [], threading.Barrier(6)

        def reset():
            barrier.wait()
            try:
                self.service.reset(NS)
            except ServiceBusy:
                pass
            except Exception as exc:  # noqa: BLE001 - surfaced by the assertion below
                errors.append(exc)

        threads = [threading.Thread(target=reset) for _ in range(6)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        self.assertEqual(errors, [])
        active = self.service.snapshot(NS)
        self.assertIn(active["run"], self.run_numbers())
        b = self.assert_invariants()
        self.assertEqual(b["cash_cents"], 100000)

    def test_purge_preserves_generation_so_captured_events_stay_stale(self):
        self.submit("PAY-001")
        old_run = self.service.meta(NS)["run"]
        stale = self.provider.settlement_event(NS, self.payment("PAY-001")["provider_idempotency_key"])
        purged = self.service.purge(NS)
        self.assertGreater(purged["next_run"], old_run)
        self.assertEqual(self.run_numbers(), set())
        with self.assertRaises(NotFound):
            self.service.snapshot(NS)
        with self.assertRaises(NotFound):
            self.deliver(stale)
        reset = self.service.reset(NS)
        self.assertEqual(reset["run"], purged["next_run"])
        recreated = self.submit("PAY-001")["payment"]
        self.assertNotEqual(recreated["provider_idempotency_key"], stale["client_reference"])
        with self.assertRaises(RejectedEvent) as ctx:
            self.deliver(stale)
        self.assertIn("stale_run", str(ctx.exception))
        self.assertEqual(self.payment("PAY-001")["status"], "SUBMITTED")
        b = self.assert_invariants()
        self.assertEqual((b["cash_cents"], b["reserved_cents"]), (100000, 12500))
        self.assertEqual(self.settle("PAY-001")["outcome"], "APPLIED")
        self.assertEqual(self.assert_invariants()["cash_cents"], 87500)

    def test_purge_fences_in_flight_submission_responses(self):
        def purge_mid_flight(submission, payment):
            self.provider.after_accept.clear()
            self.service.purge(NS)

        self.provider.after_accept.append(purge_mid_flight)
        try:
            self.submit("PAY-001")
        except NotFound:
            pass
        self.assertEqual(self.run_numbers(), set())
        self.service.reset(NS)
        snap = self.service.snapshot(NS)
        self.assertEqual((snap["payments"], snap["journal"]), ([], []))
        self.assertEqual(self.assert_invariants()["reserved_cents"], 0)

    def test_fractional_and_nested_invalid_events_are_rejected_with_evidence(self):
        self.submit("PAY-001")
        valid = self.provider.settlement_event(NS, self.payment("PAY-001")["provider_idempotency_key"])
        invalid = [dict(valid, event_id="evt_fractional_1", amount_cents=1.5),
                   dict(valid, event_id="evt_nested_float", fee={"rate": 0.25, "tiers": [1.5, None]})]
        for event in invalid:
            with self.subTest(event_id=event["event_id"]):
                with self.assertRaises(RejectedEvent):
                    self.deliver(event)
                delivery = self.service.snapshot(NS)["deliveries"][-1]
                self.assertEqual((delivery["event_id"], delivery["outcome"]), (event["event_id"], REJECTED_EVENT))
                self.assertEqual(json.loads(delivery["payload_json"]), event)
        self.assertEqual(self.service.snapshot(NS)["journal"], [])
        self.assertEqual(self.balance()["cash_cents"], 100000)
        self.assertEqual(self.deliver(valid)["outcome"], "APPLIED")
        self.assert_invariants()

    # Storage guarantees ----------------------------------------------------------------------

    def test_journal_entries_are_insert_only(self):
        self.submit("PAY-001")
        self.settle("PAY-001")
        entry = self.service.snapshot(NS)["journal"][0]
        sk = f"RUN#00001#JOURNAL#{entry['entry_id']}"
        with self.assertRaises(ConditionFailed):
            self.store.transact([Put(f"NS#{NS}", sk, dict(entry, amount_cents=1))])
        self.assertEqual(self.service.snapshot(NS)["journal"][0], entry)

    def test_transaction_conflicts_are_retried(self):
        conflicts = []

        def conflict_twice(ops):
            if any("#IDEM#" in op.sk for op in ops) and len(conflicts) < 2:
                conflicts.append(True)
                raise Contention("TransactionConflict (injected)")

        self.store.before_transact.append(conflict_twice)
        result = self.submit("PAY-001")
        self.assertEqual(len(conflicts), 2)
        self.assertEqual(result["payment"]["status"], "SUBMITTED")
        self.assertEqual(self.balance()["reserved_cents"], 12500)

    def test_full_demo_through_http_router(self):
        def post(path, body=None, headers=None):
            status, _, raw, _ = self.app.handle("POST", f"/api/namespaces/{NS}{path}", headers or {},
                                                json.dumps(body or {}).encode())
            return status, json.loads(raw)

        self.assertEqual(post("/demo/submit-fixtures")[0], 200)
        self.assertEqual([r["outcome"] for r in post("/demo/settle-all")[1]["results"]], ["APPLIED"] * 3)
        self.assertEqual(post("/demo/replay-settlement")[1]["results"][0]["outcome"], "DUPLICATE_EVENT")
        self.assertEqual(post("/demo/return-pay-002")[1]["results"][0]["outcome"], "APPLIED")
        self.assertEqual(post("/demo/replay-return")[1]["results"][0]["outcome"], "DUPLICATE_EVENT")
        status, _, raw, _ = self.app.handle("GET", f"/api/namespaces/{NS}/reconciliation", {}, b"", "stage=after_return")
        self.assertEqual(json.loads(raw)["status"], "PASS")
        self.assertEqual(self.app.handle("GET", "/api/nope")[0], 404)
        self.assertEqual(self.app.handle("POST", f"/api/namespaces/{NS}/payments", {}, b"x" * 20000)[0], 413)
        self.assertEqual(self.app.handle("GET", "/api/namespaces/prod-bank/state")[0], 400)
        status, body = post("/payments", self.request("PAY-009"), {"Idempotency-Key": "http-key-0009"})
        self.assertEqual((status, body["payment"]["status"]), (201, "SUBMITTED"))

    def test_http_router_rejects_malformed_bodies_without_changing_state(self):
        self.submit("PAY-001")
        self.settle("PAY-001")
        before = self.service.snapshot(NS)
        cases = [("/reset", b"[]"), ("/reset", b"null"), ("/reset", b'"x"'), ("/reset", b"7"),
                 ("/reset", b'{"opening_cash_cents": 1.5}'), ("/purge", b"[]"), ("/purge", b"null"),
                 ("/payments", b"[]"), ("/demo/submit-fixtures", b"null"),
                 ("/demo/duplicate-settlement", b'{"variant": "abc"}'),
                 ("/demo/duplicate-settlement", b'{"variant": 1.5}'),
                 ("/demo/duplicate-settlement", b'{"variant": true}'),
                 ("/demo/duplicate-settlement", b'{"variant": -1}'),
                 ("/demo/duplicate-settlement", b'{"payment_id": ["PAY-001"]}'),
                 ("/demo/replay-settlement", b'{"payment_id": 7}'),
                 ("/demo/replay-return", b'{"payment_id": null}'),
                 ("/demo/provider-event", b'{"type": "settled", "payment_id": "PAY-001", "variant": "x"}'),
                 ("/demo/provider-event", b'{"type": "returned", "payment_id": "PAY-001", "return_code": 5}'),
                 ("/demo/provider-event", b'{"type": "returned"}'),
                 ("/demo/redeliver", b'{"event_id": {"a": 1}}'),
                 ("/demo/arm-timeout", b"[]")]
        for path, raw in cases:
            with self.subTest(path=path, body=raw):
                status, _, out, _ = self.app.handle("POST", f"/api/namespaces/{NS}{path}",
                                                    {"Idempotency-Key": "malformed-key-1"}, raw)
                self.assertEqual(status, 400)
                self.assertIn("code", json.loads(out)["error"])
        self.assertEqual(self.service.snapshot(NS), before)

    def test_http_router_returns_structured_500_for_unexpected_errors(self):
        with mock.patch.object(self.service, "snapshot", side_effect=RuntimeError("boom")), \
                self.assertLogs("modern.api", "ERROR"):
            status, _, out, _ = self.app.handle("GET", f"/api/namespaces/{NS}/state")
        self.assertEqual((status, json.loads(out)["error"]["code"]), (500, "internal_error"))
        self.assertNotIn("boom", out.decode())

    def test_lambda_handler_routes_http_api_events(self):
        import modern.lambda_handler as handler_module
        with mock.patch.object(handler_module, "_APP", self.app):
            response = handler_module.handler({
                "rawPath": f"/api/namespaces/{NS}/payments", "rawQueryString": "",
                "headers": {"idempotency-key": "lambda-key-001", "content-type": "application/json"},
                "requestContext": {"http": {"method": "POST"}, "requestId": "r1"},
                "body": json.dumps(self.request("PAY-001")), "isBase64Encoded": False}, None)
        self.assertEqual(response["statusCode"], 201)
        self.assertEqual(json.loads(response["body"])["payment"]["status"], "SUBMITTED")


class SqliteBehaviorTests(BehaviorSuite, unittest.TestCase):
    def make_store(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.path = Path(self.tmp.name) / "modern.sqlite"
        store = SqliteStore(self.path)
        self.addCleanup(store.close)
        return store

    def reopen_store(self):
        self.store.close()
        self.store = SqliteStore(self.path)
        self.addCleanup(self.store.close)
        return self.store

    def test_sqlite_trigger_blocks_journal_updates(self):
        self.submit("PAY-001")
        self.settle("PAY-001")
        conn = self.store._conn()
        with self.assertRaises(sqlite3.IntegrityError):
            conn.execute("UPDATE items SET data='{}' WHERE sk LIKE '%#JOURNAL#%'")



class AtomicRequestClient:
    """Serialize individual moto requests.

    Real DynamoDB applies each request atomically. moto does not under threads: a failed
    transaction restores a deep copy of every table, erasing other threads' committed writes.
    One lock per request restores DynamoDB's per-request atomicity while service-level reads
    and conditional writes still interleave across threads.
    """

    lock = threading.RLock()

    def __init__(self, client):
        self._client = client

    def __getattr__(self, name):
        attr = getattr(self._client, name)
        if not callable(attr):
            return attr

        def call(*args, **kwargs):
            with self.lock:
                return attr(*args, **kwargs)
        return call


@unittest.skipIf(mock_aws is None, "moto/boto3 not installed (pip install -r requirements-dev.txt)")
class DynamoBehaviorTests(BehaviorSuite, unittest.TestCase):
    TABLE = "demo-table"

    def make_store(self):
        env = mock.patch.dict(os.environ, {"AWS_DEFAULT_REGION": "us-east-1", "AWS_ACCESS_KEY_ID": "testing",
                                           "AWS_SECRET_ACCESS_KEY": "testing"})
        env.start()
        self.addCleanup(env.stop)
        aws = mock_aws()
        aws.start()
        self.addCleanup(aws.stop)
        boto3.client("dynamodb").create_table(
            TableName=self.TABLE, BillingMode="PAY_PER_REQUEST",
            KeySchema=[{"AttributeName": "pk", "KeyType": "HASH"}, {"AttributeName": "sk", "KeyType": "RANGE"}],
            AttributeDefinitions=[{"AttributeName": "pk", "AttributeType": "S"},
                                  {"AttributeName": "sk", "AttributeType": "S"}])
        from modern.store import DynamoStore
        self.client = AtomicRequestClient(boto3.client("dynamodb"))
        return DynamoStore(self.TABLE, client=self.client)

    def reopen_store(self):
        from modern.store import DynamoStore
        self.store = DynamoStore(self.TABLE, client=AtomicRequestClient(boto3.client("dynamodb")))
        return self.store


@unittest.skipIf(boto3 is None, "boto3 not installed")
class DynamoErrorMappingTests(unittest.TestCase):
    """TransactionCanceledException reasons map to ConditionFailed(index) or retryable Contention."""

    def store_raising(self, reasons):
        from botocore.exceptions import ClientError
        from modern.store import DynamoStore

        class FakeClient:
            calls = []

            def transact_write_items(self, **kwargs):
                self.calls.append(kwargs)
                raise ClientError({"Error": {"Code": "TransactionCanceledException", "Message": "cancelled"},
                                   "CancellationReasons": [{"Code": c} for c in reasons]}, "TransactWriteItems")

        return DynamoStore("t", client=FakeClient())

    def test_condition_failure_reports_index(self):
        store = self.store_raising(["None", "ConditionalCheckFailed"])
        with self.assertRaises(ConditionFailed) as ctx:
            store.transact([Put("p", "a", {"x": 1}), Put("p", "b", {"x": 2})])
        self.assertEqual(ctx.exception.index, 1)

    def test_transaction_conflict_is_retryable(self):
        store = self.store_raising(["TransactionConflict", "None"])
        with self.assertRaises(Contention):
            store.transact([Put("p", "a", {"x": 1}), Put("p", "b", {"x": 2})])

    def test_no_client_request_token_is_relied_on(self):
        store = self.store_raising(["TransactionConflict"])
        with self.assertRaises(Contention):
            store.transact([Put("p", "a", {"x": 1})])
        self.assertNotIn("ClientRequestToken", store.client.calls[-1])
        self.assertEqual(store.client.calls[-1]["TransactItems"][0]["Put"]["ConditionExpression"],
                         "attribute_not_exists(#pk)")


class InfrastructureTemplateTests(unittest.TestCase):
    """Static guardrails on infra/app.json (no AWS calls)."""

    def setUp(self):
        self.template = json.loads((ROOT / "infra" / "app.json").read_text())
        self.resources = self.template["Resources"]

    def of_type(self, kind):
        return [r["Properties"] for r in self.resources.values() if r["Type"] == kind]

    def test_every_route_requires_iam(self):
        routes = self.of_type("AWS::ApiGatewayV2::Route")
        self.assertTrue(routes)
        self.assertTrue(all(r["AuthorizationType"] == "AWS_IAM" for r in routes))

    def test_no_always_on_or_network_resources(self):
        forbidden = ("AWS::EC2::", "AWS::RDS::", "AWS::EKS::", "AWS::ECS::", "AWS::ElastiCache::",
                     "AWS::DynamoDB::Table", "AWS::S3::Bucket")
        kinds = {r["Type"] for r in self.resources.values()}
        self.assertFalse([k for k in kinds if k.startswith(forbidden)], kinds)
        self.assertNotIn("VpcConfig", self.of_type("AWS::Lambda::Function")[0])

    def test_logs_have_retention_and_function_is_scoped(self):
        self.assertTrue(all(g.get("RetentionInDays") == 7 for g in self.of_type("AWS::Logs::LogGroup")))
        statements = self.of_type("AWS::IAM::Role")[0]["Policies"][0]["PolicyDocument"]["Statement"]
        table = next(s for s in statements if s["Sid"] == "DemoNamespacesOnly")
        self.assertNotIn("*", json.dumps(table["Resource"]).replace("${", ""))
        self.assertEqual(table["Condition"]["ForAllValues:StringLike"]["dynamodb:LeadingKeys"], ["NS#demo-*"])
        self.assertNotIn("dynamodb:Scan", table["Action"])
        fn = self.of_type("AWS::Lambda::Function")[0]
        self.assertEqual(fn["Runtime"], "python3.13")


if __name__ == "__main__":
    unittest.main()
