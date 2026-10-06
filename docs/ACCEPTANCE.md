# Modernization acceptance criteria

## Preserve the starting evidence

Keep the original legacy implementation, tests and golden fixture unchanged. The supplied baseline is independently checkable. Add modern code and tests separately. Store monetary amounts as integer cents.

## Mandatory payment behavior

1. Run the original three-payment scenario on the modern implementation. Final balances, payment statuses and journal totals must match the supplied golden result.
2. Accept an idempotency key on payment submission. Identical retries return the original payment without another provider submission or journal posting. Reusing a key with a different payload produces a clear conflict.
3. Persist state across process restart. Verify concurrent submissions with the same key produce one logical payment.
4. Process provider settlement events. Replaying an event ID, or receiving a distinct event ID for the same settlement, must not double post.
5. Return PAY-002 for a simulated insufficient-funds reason after settlement. Post one compensating journal entry; leave the original entries intact. Final cash is 85000 cents; net settled outflow is 15000 cents. Debit and credit totals are each 22000 cents, including the reversal.
6. Duplicate returns must not post again. Return-before-settlement delivery must produce a documented deterministic outcome, such as a pending event resolved when its dependency arrives. No unexplained dropped events.
7. Invalid amount, currency, payment reference or event must fail without partial ledger changes. Show atomic state/journal updates and an insufficient-cash test.
8. A provider timeout after it accepted a submission must not cause a second payment on retry. Use provider idempotency or reconciliation; demonstrate this with the simulator.

Define explicitly when funds are reserved, when cash is posted, and which transitions are allowed. Do not use an in-memory lock as the only AWS concurrency protection. Describe remaining production limitations honestly.

## Demo and evidence

- A minimal dashboard shows initial cash, payment statuses, immutable journal entries, reconciliation differences and event history.
- Actions: reset a synthetic demo namespace, submit fixtures, settle, replay a duplicate, return PAY-002, replay the return. Reset must be limited to the selected synthetic namespace.
- A five-minute runbook includes the legacy baseline and modern steps. Show results and failure handling, not just generated code.
- CI tests the required scenarios. Add a machine-readable report with expected/actual balances and statuses and zero unexplained differences.
- Provide an open PR, summary of validation, and links to the completed Devin session supplied by the operator.

## AWS and provider boundaries

- Required provider: deterministic local simulator. No real accounts, real money, secrets or personal data.
- Deployable target: API Gateway HTTP API, Python Lambda and on-demand DynamoDB. Runtime access is limited to the selected table; logs have explicit retention. Provide infrastructure and teardown instructions.
- Document public demo endpoint authentication, request validation and deployment limitations. Do not expose an unauthenticated mutating endpoint simply for convenience.
- Keep local tests runnable without an AWS account. Deployment is a distinct check; do not call it deployed until an actual endpoint has been exercised.
- Optional Column sandbox: implement only if approved credentials are available through a supported secret integration. Verify the current API and webhook authentication specifications. Never treat a simulated adapter as a live Column, Thought Machine or Finxact integration.
- Explain which components could run in a customer's VPC, what leaves that boundary, and what would require review. Do not claim this proves that Devin itself runs inside the bank's VPC.
