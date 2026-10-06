# Modern payment integration: design

This is an integration and reconciliation layer next to a banking core. It is not a core
replacement. The legacy batch (`legacy/batch.py`) is unchanged and is still the reference result.

## Components

| Module | Responsibility |
| --- | --- |
| `modern/store.py` | One conditional-transaction interface with two backends: `SqliteStore` (local, stdlib) and `DynamoStore` (`TransactWriteItems`). |
| `modern/service.py` | Payment subledger: submission idempotency, leases, provider outcomes, event ingestion, journal posting, run generations. |
| `modern/provider.py` | `PaymentProvider` protocol and the deterministic `SimulatedProvider`, which has provider-side idempotency and fault injection. |
| `modern/webhook.py` | HMAC-SHA256 webhook signatures (the simulator's scheme). |
| `modern/reconciliation.py` | Expected-vs-actual report, balance invariants, and a provider-vs-subledger comparison. |
| `modern/api.py` | HTTP router shared by the local server and the Lambda handler. |
| `modern/server.py`, `modern/dashboard.html` | Local dashboard. Serves SQLite directly, or acts as a SigV4 signing proxy to the IAM-protected API. |
| `modern/demo.py` | Repeatable CLI demo plus edge-case run, local or remote. |

## State machine

```
                 provider accepts                settlement event          return event
PENDING_SUBMISSION ────────────────▶ SUBMITTED ─────────────────▶ SETTLED ─────────────▶ RETURNED
   │   │   ▲  timeout / unavailable                                   ▲
   │   │   └── stays PENDING, reservation kept; retry or reconcile-pending
   │   └── settlement event wins the race ────────────────────────────┘ (PENDING → SETTLED)
   └── provider definitively rejects ─▶ REJECTED (reservation released, no journal)
Return before settlement: parked on the payment; applied in the same transaction as the settlement.
```

The allowed transitions are listed in `TRANSITIONS` in `modern/service.py`. Anything else is
refused. `RETURNED` and `REJECTED` are terminal.

## Ledger semantics and invariants

All amounts are integer cents. The balance item holds `cash`, `reserved` and `available`.

| Step | cash | reserved | available | Journal |
| --- | --- | --- | --- | --- |
| Reserve (submission) | 0 | +amount | −amount | none |
| Settle | −amount | −amount | 0 | DEBIT `payment_outflow`, CREDIT `cash` |
| Return | +amount | 0 | +amount | DEBIT `cash`, CREDIT `payment_outflow` |
| Reject (definitive) | 0 | −amount | +amount | none |

Invariants:
- `available = cash − reserved`, with `available ≥ 0` and `reserved ≥ 0`. The non-negativity
  guards are conditions on the increment itself, so an overdraft cannot commit.
- Total debits equal total credits.
- Cash equals opening cash plus the net of cash journal lines.
- Reserved equals the sum of in-flight payments (`PENDING_SUBMISSION` and `SUBMITTED`).
- Each payment's journal effects match its status.

`modern/reconciliation.py` checks all of these on every report.

Each state change is **one atomic transaction**. It writes the payment state, both journal
lines, the balance delta, the event record and the delivery history together, and it pins the
active run with a `Check` on the namespace `META` item.

Journal entry IDs are deterministic (`PAY-001#SETTLEMENT#DEBIT`) and written with insert-only
`Put` (`attribute_not_exists`). A second posting of the same effect therefore fails its
condition and can never overwrite an entry. SQLite also has an update-blocking trigger on
journal rows.

DynamoDB forbids two actions on the same item in one transaction. When a settlement resolves a
parked return, both balance deltas are merged into **one** `Update` on the balance item by
`merge_increments`. The store rejects any transaction that touches an item twice, on both
backends.

## Submission idempotency, leases and timeout recovery

1. `POST /payments` requires `Idempotency-Key`. The request is validated and canonicalized
   (sorted-key JSON), then hashed.
2. One transaction does three things: it claims `IDEM#<key>` (storing the hash and the
   canonical payload), creates the payment in `PENDING_SUBMISSION`, and reserves funds.
3. Reusing a key with the same payload replays the stored payment. A different payload
   returns `409 idempotency_key_conflict`. Reusing a `payment_id` under a different key
   returns `409`.
4. The worker takes a **lease**: a random `lease_owner` and a `lease_until` timestamp, written
   with a versioned `Replace` (`_v` must match). It then calls the provider with a provider
   idempotency key scoped to `namespace:run:payment_id`.
5. The provider outcome is recorded only if this worker still owns the lease and the version is
   unchanged. If either has moved on (a newer worker, a settlement that won the race, or a
   reset), the response is ignored (`late_provider_response_ignored`). A late acceptance or
   rejection can never downgrade `SETTLED`/`RETURNED` or release funds twice.
6. A timeout or an unavailable provider is **ambiguous**. The payment stays
   `PENDING_SUBMISSION`, keeps its reservation, and the lease is cleared.
7. Recovery after a timeout or crash is explicit: `POST /reconcile-pending` (or
   `python3 -m modern.demo reconcile-pending`). For each pending payment whose lease is free
   or expired, it first **looks up** the provider by the scoped idempotency key, and resubmits
   with the same key only if the provider has no record.

The lease only limits concurrent work. It cannot guarantee a single HTTP call to the provider:
a crashed worker may have sent its request before its lease expired. Provider-side idempotency
is what prevents a duplicate payment.

DynamoDB's `ClientRequestToken` is deliberately not used. It only protects for ten minutes.
Durable idempotency comes from stored records and conditional writes.

## Provider events

Every delivery is processed as follows:
1. The signature is verified.
2. The full schema is validated (type, IDs, integer amount, currency, reference, return code).
   This happens **before** any duplicate-effect classification, so an invalid event is always
   rejected rather than reported as a duplicate.
3. The event ID is looked up. If seen with the same payload hash, the outcome is
   `DUPLICATE_EVENT`. If seen with a different hash, it is rejected with `409 event_conflict`.
4. The event is resolved to the payment through `client_reference = namespace:run:payment_id`.
   Events for an old run are rejected as `stale_run`. The provider payment ID, amount and
   currency must match.
5. The event is applied:
   - settlement: `APPLIED`;
   - return after settlement: `APPLIED`;
   - return before settlement: `PARKED`;
   - effect already applied under another event ID: `DUPLICATE_EFFECT`.

Each event record stores its payload and hash. The delivery history stores every attempt,
including rejections.

When a parked return resolves, the evidence links it to the settlement:
- the return's event record becomes `APPLIED` with `resolved_by_event_id`;
- the settlement record lists `applied_parked_event_ids`;
- the return journal lines carry `resolved_with_settlement_event_id`.

## Reset and run generations

`POST /reset` bumps the run number in the selected `demo-*` namespace's `META` item and creates
a fresh balance under `RUN#<n>#`. Every write checks the active run, and provider keys embed
the run. As a result:
- delayed provider events from an old run are rejected as `stale_run`;
- in-flight submission responses from an old run fail their run check and are ignored;
- a new run can reuse `PAY-001` without colliding.

Items of strictly older runs are then purged. An overlapping reset that has already started a
newer run is never deleted by an older reset's cleanup. The simulator's history (`SIM#`) is
kept, because a real provider's history cannot be erased. Other namespaces are never read or
written.

`POST /purge` with `{"confirm": "<namespace>"}` deletes the namespace's subledger and simulator
history but keeps generation identity: `META` becomes a tombstone fenced one past the last run.
Writes pinned to an older run fail their run check, the next reset starts a run whose provider
references were never issued, and events captured before the purge are rejected as `stale_run`.

Delivery history stores each raw payload as canonical JSON text (`payload_json`, up to 4 KiB)
with its hash, so rejected events with values DynamoDB cannot type (such as fractional amounts)
still leave evidence.

## Concurrency and conflicts

- **Optimistic concurrency:** every item carries `_v`. Writes to the payment and `META` are
  conditional replaces.
- **Retries:** `DynamoStore` maps cancellation reasons. `ConditionalCheckFailed` becomes
  `ConditionFailed(index)`; `TransactionConflict` and throttling become `Contention`. The
  service retries contention with jittered backoff (up to 8 attempts). If it still cannot
  commit, it returns `503 service_busy`, and the client retries with the same key.
- **Spending the same funds:** the reservation's guarded increment serializes competing
  submissions at the balance item, so at most `floor(available / amount)` succeed.
- **SQLite:** each transaction uses `BEGIN IMMEDIATE`.
