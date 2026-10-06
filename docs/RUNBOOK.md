# Five-minute demo runbook

All data is synthetic. The provider is the deterministic simulator. Nothing here contacts a
third party or moves money.

## 0. Prerequisites (one time)

```bash
git clone https://github.com/NatorOS/banking-modernization-demo.git
cd banking-modernization-demo
git checkout devin/1791310804-event-driven-payments   # until the PR is merged
python3 --version                                       # 3.13+
```

Local mode needs only the standard library. To run the DynamoDB half of the test suite, also
install `python3 -m pip install -r requirements-dev.txt` (boto3 and moto).

## 1. Tests (about 30 s)

```bash
python3 -m unittest discover -s tests -v
python3 -m scripts.baseline
```

Expected:
- `OK`. Without moto installed, the DynamoDB classes are reported as skipped.
- `PASS: baseline matches golden values`.

## 2. Scripted demo (about 1 min)

```bash
python3 -m modern.demo run
```

Every step prints `[PASS]` and the run ends with `All checks passed.` The key lines are:

| Step | Shows | Expected |
| --- | --- | --- |
| 1 | Legacy batch | cash 81500, matches golden |
| 3 | Submit 3 fixtures | `SUBMITTED`; reserved 18500, available 81500, cash 100000, no journal |
| 4 | Client retry / key misuse | replayed; different payload → 409 |
| 5 | Settlement events | modern result equals the legacy golden result (cash 81500, debits = credits = 18500) |
| 6 | Duplicate settlement | `DUPLICATE_EVENT` (same ID), `DUPLICATE_EFFECT` (new ID), no double posting |
| 7 | Return PAY-002 | cash 85000, net settled outflow 15000, debits = credits = 22000 |
| 8 | Duplicate return | `DUPLICATE_EVENT`, `DUPLICATE_EFFECT` |
| 9 | Reconciliation | `PASS`, 0 unexplained differences, 14/14 invariants |

The report is written to `output/reconciliation.json`.

Optional edge cases (about 30 s): `python3 -m modern.demo edge-cases`. It covers invalid input,
insufficient cash, timeout after acceptance, return before settlement, invalid events, and reset
isolation (an old-run event is rejected as `stale_run`).

## 3. Dashboard (about 3 min)

```bash
python3 -m modern.server            # http://127.0.0.1:8000/
```

Keep namespace `demo-local` and click in order:

1. **Reset namespace**: starts a new run; cash $1,000.00, no payments.
2. **Submit fixtures**: three `SUBMITTED` payments; reserved $185.00, available $815.00.
3. **Settle**: modern outcome equals the legacy panel ($815.00 cash; $185.00 debits and
   credits); reconciliation shows `PASS` against the golden result.
4. **Replay settlement (same event ID)**: the event history shows `DUPLICATE_EVENT`; balances
   are unchanged.
5. **Return PAY-002 (R01)**: PAY-002 is `RETURNED`; cash $850.00; debits and credits $220.00;
   reconciliation shows `PASS` against `after_return`.
6. **Replay return**: `DUPLICATE_EVENT`; nothing changes.

The talking points are the immutable journal panel (compensating entries, never edits), the
event history (every delivery, including duplicates and rejections) and the reconciliation panel.

## Reset (synthetic data only)

- **Dashboard:** click **Reset namespace**.
- **CLI:** `python3 -m modern.demo reset --namespace demo-local`.

A reset starts a fresh run generation in that one `demo-*` namespace. Namespaces must match
`demo-[a-z0-9-]+`, so a reset cannot target anything else.

To discard all local state: `rm -rf data/ output/`. Those directories are git-ignored and hold
only synthetic data and a locally generated simulator signing key.

## AWS mode (deployed and exercised)

The operator runs these with their own scoped profile:

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements-aws.txt
bash scripts/deploy-app.sh <profile> 784620264480 us-east-1
python3 -m modern.demo run --remote <ApiUrl> --profile <profile>
python3 -m modern.server --remote <ApiUrl> --profile <profile>    # dashboard via local SigV4 proxy
bash scripts/teardown-app.sh <profile> 784620264480 us-east-1 --purge-data   # foundation preserved
```

The deploy script refuses to run if the profile's account or the region does not match. It
reads the table and bucket names from the foundation stack's outputs, and prints `403` for an
unsigned request as proof that IAM auth is enforced.

The current endpoint is `https://15ggin9fcd.execute-api.us-east-1.amazonaws.com`.
The AWS dashboard defaults to `demo-aws`; use `--namespace` to select another synthetic run.
See [AWS_VALIDATION.md](AWS_VALIDATION.md) for the verified results and commands to run the
shared suite against real DynamoDB without moto, plus concurrency through API Gateway/Lambda.
