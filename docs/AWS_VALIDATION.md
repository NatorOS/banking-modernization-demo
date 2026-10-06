# AWS deployment and verification

Verified October 6, 2026 (America/New_York). This is a synthetic simulator demo, including
when running in AWS. No Column adapter was added and no Column credential was read.

Saved evidence: [machine-readable verification](evidence/aws-validation.json) and
[AWS dashboard screenshot](evidence/aws-dashboard.jpg).

- Account: `784620264480`; region: `us-east-1`.
- Application stack: `banking-modernization-demo-app` (successful deployment/update).
- API: `https://15ggin9fcd.execute-api.us-east-1.amazonaws.com`.
- Runtime: Python 3.13, arm64, 256 MB; IAM-protected HTTP API; restricted execution role;
  seven-day Lambda log retention. Foundation table and bucket were reused.
- Deployment used the operator's existing local `natoros` login; credentials were never
  exported into application code or supplied to Devin. This login is root; the application
  uses the restricted role provisioned by CloudFormation.

## Actual checks

| Check | Result |
| --- | --- |
| Existing SQLite/moto suite plus new cancellation regression | 83 tests passed |
| Shared behavioral suite on real DynamoDB, no moto or request serialization | 36 tests passed |
| Real DynamoDB concurrency (same-key, competing spend, concurrent resets), five repetitions | 15 tests passed |
| Signed API Gateway → Lambda → DynamoDB demo | PASS 14/14; cash 85000 cents, debit/credit totals 22000 cents each |
| Remote edge-case demo | Passed: timeout recovery, reordered return, invalid input, stale-generation rejection |
| Unsigned API request / signed health | 403 / 200; health identifies AWS and DynamoDB |
| Eight simultaneous same-key API submissions | One logical payment; reserved 400 cents; reconciliation PASS |
| Eight simultaneous API submissions of 400 cents against 1000 cents | Two accepted, six insufficient-funds responses; reserved 800, available 200; PASS |

The first real DynamoDB run found a false `PaymentConflict` response during concurrent
same-key submissions. A cancellation reported the payment condition while the competing
transaction had already claimed the same idempotency key. The service now rereads the durable
claim before classifying a business conflict or insufficient funds. A shared regression covers
both payment and balance cancellation indices. The fix was redeployed before the final API
checks. No duplicate payment or ledger posting was observed.

The shared suite uses the real SDK/table and concurrent threads, with controlled clocks and
fault-injection hooks for crash/race scenarios. It runs from the operator's machine; the
remote demo and API concurrency checks separately exercise the deployed Lambda role, IAM,
routing and runtime. Neither is a claim of production readiness or exhaustive race coverage.

## Pending follow-ups

- **Tests added after this verification have not run on real DynamoDB.** Four concurrent
  duplicate-webhook tests (settlement and return, each with the same event ID and with
  distinct event IDs for one effect) and the dashboard 6b duplicate-return check have run only
  on SQLite and moto. Run the commands below to verify them; `--filter concurrent` includes them.
- **Scoped deploy role.** Deployment used the operator's root login. Replace it with a deploy
  role limited to the application stack (CloudFormation, Lambda, API Gateway, CloudWatch Logs,
  the simulator secret, and `iam:PassRole` for the Lambda execution role only). IAM has not
  been changed yet.

## Repeat verification

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements-aws.txt
python scripts/test_real_dynamodb.py --profile natoros \
  --expected-account 784620264480 --region us-east-1 \
  --table banking-modernization-demo-foundation-DemoTable-1712SBKSQJZXT
python scripts/test_real_dynamodb.py --profile natoros \
  --expected-account 784620264480 --region us-east-1 \
  --table banking-modernization-demo-foundation-DemoTable-1712SBKSQJZXT \
  --filter concurrent --repeat 5 --output output/real-dynamodb-concurrency.json
python scripts/verify_aws_api.py --profile natoros --expected-account 784620264480
python -m modern.demo run --remote https://15ggin9fcd.execute-api.us-east-1.amazonaws.com \
  --profile natoros --namespace demo-aws
python -m modern.demo edge-cases --remote https://15ggin9fcd.execute-api.us-east-1.amazonaws.com \
  --profile natoros --namespace demo-aws
python -m modern.server --remote https://15ggin9fcd.execute-api.us-east-1.amazonaws.com \
  --profile natoros --namespace demo-aws --port 8001
```

Open `http://127.0.0.1:8001/` for the dashboard. The browser talks to a loopback proxy that
signs requests using the local profile. Credentials do not enter the browser.
Refresh the local AWS login if it expires; the endpoint itself remains deployed.

The SDK needs the CRT dependency for the AWS CLI login credential provider; the original
test-only boto3 pin is insufficient for this operator login. `requirements-aws.txt` installs
the current login-capable SDK and CRT without changing Lambda's runtime dependency model.

Reports are written under ignored `output/`: `real-dynamodb-tests.json`,
`real-dynamodb-concurrency.json`, `aws-api-verification.json`, and `reconciliation.json`.
Direct tests generate random `demo-live-*` partitions and delete only those partitions.
API tests generate a random `demo-api-*` namespace and purge its data while preserving its
generation tombstone. The interactive `demo-aws` namespace remains available for the demo.

## Cleanup

```bash
bash scripts/teardown-app.sh natoros 784620264480 us-east-1 --purge-data
```

This deletes the application and its simulator signing secret while preserving the foundation
stack. The optional Column secret is a separate resource and is not touched. Resources remain
deployed until explicitly torn down and can incur usage/storage charges; no cost estimate was
inferred from the test counts.
