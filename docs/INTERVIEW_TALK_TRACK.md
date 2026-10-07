# Five-minute interview talk track

**One rule throughout:** the payment provider is a deterministic simulator in this repo. It uses signed HMAC webhooks, touches no payment rails and moves no money. The AWS infrastructure is real: it is deployed in account `784620264480`, `us-east-1` (see [AWS_VALIDATION.md](AWS_VALIDATION.md)). No Column or other core provider is connected.

**Before the call:**
- Open [the hosted dashboard](https://banking-modernization-demo.vercel.app) and sign in to Vercel. It uses the real AWS backend in `demo-aws`; the provider remains simulated.
- AWS fallback: refresh the AWS login. Build the Arc UI once (`cd web && npm ci && npm run build`), then start `python -m scripts.dashboard --remote https://15ggin9fcd.execute-api.us-east-1.amazonaws.com --profile natoros --namespace demo-aws --port 8001` and open `http://127.0.0.1:8001/`.
- Fallback: run the local dashboard (`python -m scripts.dashboard`, `demo-local`); without a build, `modern.server` serves the classic page. The steps and numbers are identical.

## 0:00–0:30 · Framing
- This is an integration and reconciliation layer around a modern banking core, not a core replacement.
- All data is synthetic. The provider is simulated. The serverless stack is real.

## 0:30–1:15 · Legacy baseline (left panel)
- `legacy/batch.py` is one end-of-day SQLite transaction. It takes three queued payments (PAY-001 $125, PAY-002 $35, PAY-003 $25), checks cash for the whole batch, posts debits and credits, and marks them SETTLED.
- Golden result: cash $815.00, with debits and credits of $185.00 each.
- It has no provider, no asynchronous events and no returns. That is the gap this demo fills.
- The legacy code, its tests and the golden fixture are unchanged. The modern result has to reproduce them.

## 1:15–2:15 · Event-driven flow (buttons 1 → 2 → 2b → 3)
- **1 Reset:** starts a fresh run generation in this one `demo-*` namespace. Events from an older run can never affect the new one.
- **2 Submit:** one conditional transaction does three things:
  - claims the Idempotency-Key, which is bound to a hash of the canonical payload;
  - creates the payment as `PENDING_SUBMISSION`;
  - reserves the funds, guarded so no balance goes negative.

  No journal rows yet: reserved $185, available $815.
- **Provider call:** the provider is called with a key scoped to the namespace, run and payment.
  - On a timeout, the reservation is kept and the payment stays pending.
  - `reconcile-pending` asks the provider before resubmitting. Provider idempotency, not a lock, is what prevents a second payment.
- **2b Retry:** the same requests replay. No new payment and no new reservation. Reusing a key with a different payload returns 409.
- **3 Settle:** each signed settlement event commits five things in one transaction: payment state, two insert-only journal lines, the balance change and the event record. Result: $815 cash, $185 debits and $185 credits, matching the legacy panel.

## 2:15–3:00 · Duplicate settlement (buttons 4, 4b)
- **4 Same event ID again:** `DUPLICATE_EVENT`.
- **4b New event ID for the same effect:** `DUPLICATE_EFFECT`.
- The journal still has six rows and the balances are unchanged.
- Events are validated before duplicate classification. A reused event ID with different content returns 409. Every delivery stays in the event history, including duplicates and rejections.

## 3:00–3:40 · Return and duplicate return (buttons 5, 6, 6b)
- **5 Return PAY-002 (R01):** posts a compensating entry (DEBIT cash / CREDIT payment_outflow). The original entries are never edited.
  - Result: cash $850, debits and credits $220 each, net outflow $150, eight journal rows.
- **6 Replay return:** `DUPLICATE_EVENT`.
- **6b Duplicate return:** a new simulated return event for the same payment gives `DUPLICATE_EFFECT`. Still $850, $220 and eight rows.
- A return that arrives before its settlement is parked. It is applied in the same transaction as the settlement, with evidence linking the two.

## 3:40–4:10 · Reconciliation (panel)
- `PASS` 14/14 with 0 unexplained differences. The checks include:
  - available = cash − reserved, and nothing is negative;
  - debits = credits;
  - provider status matches ledger status;
  - after settlement, the expected values equal the legacy golden result.

## 4:10–4:45 · Real AWS verification
- **The stack:**
  - API Gateway HTTP API with IAM auth; an unsigned request gets 403.
  - A Python 3.13 Lambda under a restricted execution role.
  - DynamoDB transactions on an on-demand table.
  - The hosted dashboard uses Vercel OIDC → AWS STS → SigV4 to reach the API with short-lived credentials. The local fallback uses your AWS profile through a local signing proxy; credentials never enter the browser.
- **Verified (details in [AWS_VALIDATION.md](AWS_VALIDATION.md)):**
  - The 36-test shared behavioral suite passed on real DynamoDB, with no emulator.
  - 15 concurrency runs passed.
  - The signed demo reached PASS 14/14 at $850 cash.
  - Through the live API, eight simultaneous same-key submissions produced one payment, and eight $4 payments against $10 accepted exactly two.
- **What real DynamoDB caught that the emulator didn't:** a false `PaymentConflict` in a same-key race, because the cancellation blamed a different item's condition. The service now rereads the durable claim before classifying the failure. A regression test covers it, the fix is redeployed, and the deployed package hash matches a local build of this branch.
- **Duplicate webhooks on real DynamoDB:** six concurrent copies of a settlement or a return, with the same event ID or distinct IDs, post exactly one effect. 35 concurrency runs passed, 20 of them the new webhook scenarios. On the AWS-backed dashboard, 6b returned `DUPLICATE_EFFECT` and left $850, $220/$220, eight rows and PASS 14/14 unchanged.
- **Hosted walkthrough:** the protected Vercel deployment completed steps 1 → 6b against real AWS at PASS 14/14, $850 cash, $220 debits/credits and eight journal rows. Devin measured 0.6–1.1 seconds per action including the state refresh in one walkthrough; this is an observation, not a performance benchmark. [Evidence and route checks](https://github.com/NatorOS/banking-modernization-demo/pull/5) are in PR #5.
- **What stays simulated:** the provider runs inside that Lambda. AWS here proves transactions, concurrency and IAM boundaries, not a bank integration.

## 4:45–5:00 · Partner roles and close
- **Bank:** owns the ledger, risk, regulatory accountability and the AWS account; signs off reconciliation and cutover.
- **Implementation consultancy:** maps batch behavior to events, builds the operating model and exception handling, and drives testing and cutover.
- **Core provider:** the system of record and the payment rails. Its documented API, idempotency and signed events define the real adapter. Here it is simulated.
- **Cognition (Devin):** builds the adapter, ledger logic, race tests, infrastructure-as-code and runbooks as reviewable PRs, using only the scoped access the bank grants.
- **AWS:** the managed serverless runtime, transactions and IAM boundaries.
- **Still needed for a real bank:**
  - the provider's contract-verified adapter and a signature-only webhook route;
  - a scoped deploy role in place of the root login used for this deployment;
  - alarms, a threat model and the bank's controls.
