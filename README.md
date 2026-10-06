# Banking modernization: batch to events

A synthetic banking integration demo for an AWS product partnerships case. The starter is a working Python/SQLite batch processor. Devin's assignment is to build an event-driven payment adapter with durable reconciliation and deployable AWS infrastructure.

This is a toy payment subledger, not a production banking core. All customers, payments and balances are synthetic. The required demo uses a deterministic simulated provider; any real sandbox integration must be labeled separately.

## Run the starting point

Python 3.13 or newer; no third-party dependencies are required for the starter.

```sh
python3 -m unittest discover -s tests -v
python3 -m scripts.baseline
python3 -m legacy.batch seed
python3 -m legacy.batch run
python3 -m legacy.batch report
```

The three fixture payments total $185.00. Starting cash is $1,000.00; after settlement it is $815.00. Re-running the batch must not post again.

## Give this to Devin

Connect this repository to your Devin workspace, then paste [the task prompt](docs/DEVIN_PROMPT.md). The prompt deliberately leaves the modernization implementation to Devin. Keep its completed session and resulting PR for the interview.

See [acceptance criteria](docs/ACCEPTANCE.md) and [AWS setup](docs/SETUP.md). `infra/foundation.json` provisions only a private artifact bucket and an on-demand DynamoDB table. It does not contain a modern application or establish a real banking-provider connection.

## Partnership thesis

An AWS partner can use Devin to accelerate the integration, testing and reconciliation work around a bank's move to a modern core. The bank or implementation partner owns the transformation; a core provider owns its core; this demo addresses the surrounding engineering work. Potential partners are examples, not claimed relationships or verified integrations.

The proposed modern path is HTTP API → Lambda → DynamoDB, with a payment-provider adapter and event replay. AWS consumption comes from requests, execution, durable state and logs. This repository makes no claim about Marketplace eligibility, contract credits or negotiated pricing.

## Modern event-driven integration

The modern path is added alongside the unchanged legacy batch:
- a provider interface and deterministic simulator;
- durable idempotency, signed settlement and return events, and duplicate and out-of-order
  handling;
- balanced immutable journal entries;
- reconciliation against the golden results;
- local SQLite mode and a deployable HTTP API → Lambda → DynamoDB mode.

```bash
python3 -m unittest discover -s tests -v   # legacy + modern suites (DynamoDB half needs requirements-dev.txt)
python3 -m modern.demo run                 # scripted five-minute demo, writes output/reconciliation.json
python3 -m modern.server                   # dashboard at http://127.0.0.1:8000/
```

- [docs/RUNBOOK.md](docs/RUNBOOK.md): five-minute demo, expected results, reset
- [docs/DESIGN.md](docs/DESIGN.md): state machine, ledger semantics, idempotency, leases, reset generations
- [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md): diagram and partner roles
- [docs/CONSUMPTION.md](docs/CONSUMPTION.md): AWS services, usage drivers, measured vs estimated
- [docs/SECURITY.md](docs/SECURITY.md): credentials, webhooks, network, egress, logging, real-bank gaps

AWS deployment status: **pending**. The stack (`infra/app.json`) and the
`scripts/deploy-app.sh` / `scripts/teardown-app.sh` scripts are ready for an operator with
scoped access. The Column adapter is omitted until scoped access is available.
