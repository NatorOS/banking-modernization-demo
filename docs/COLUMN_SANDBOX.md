# Column sandbox integration brief

The operator has created a Column sandbox and stored its API key in AWS Secrets Manager: `arn:aws:secretsmanager:us-east-1:784620264480:secret:banking-modernization-demo/column-sandbox-IZrTpn`. On October 6, 2026, a read-only `GET /bank-accounts?limit=1` request returned HTTP 200 with an empty account list. Authentication is verified; no payment flow has been executed. Credentials are not stored in this repository or provisioned to Devin. The mandatory deterministic simulator remains the reproducible correctness test; a verified Column sandbox flow is the desired external-provider demonstration once access is ready.

## Verified documentation entry points

- [Getting started](https://docs.column.com/guides/getting-started): entity, account and transfer workflow.
- [Sandbox and testing](https://docs.column.com/guides/sandbox-and-testing): sandbox uses the same API domain as production; the API key selects the environment. Sandbox keys begin with `test_`. Settlement simulation and ACH return simulations are available.
- [API key security](https://docs.column.com/guides/api-key-security/): keys are managed through the dashboard and bound to a platform and mode.
- [Idempotency](https://docs.column.com/working-with-the-api/idempotency/): verify provider semantics when implementing retries.
- [Events and webhooks](https://docs.column.com/working-with-the-api/events-and-webhooks/): verify current event names, payloads and authenticity checks.

Reject keys without the sandbox prefix before any request. Do not print the key or save it in public files. Configure an AWS Secrets Manager secret or approved Devin secret integration, then supply only the secret identifier in task context. Permission to read that secret must be scoped to its exact ARN.

## Desired sandbox demonstration

Create synthetic entity/account/counterparty fixtures using the documented sandbox workflow. Fund a synthetic source account through supported sandbox mechanisms. Originate an ACH transfer, advance its settlement using the sandbox endpoint, and reconcile its provider state with the adapter's subledger.

Then exercise the documented return simulation. The sandbox documentation offers receiver-name magic values, including `RETURN_NSF` for R01 and `RETURN_ACCOUNT_CLOSED` for R02. Confirm the observed order of events in the sandbox; do not force the provider to match the local simulator's ordering. Demonstrate duplicate/reordered delivery using captured, sanitized sandbox event fixtures or a clearly labeled replay tool, not a fabricated claim that Column sent those duplicates.

Use a separate namespace for each run and record actual account/transfer identifiers only in ignored local outputs or runtime configuration. A logical demo reset starts a fresh namespace; it should not claim to erase external payment history.

## Evidence before calling this integrated

Record successful sandbox authentication, created synthetic resources, an actual submitted transfer, observed settlement/return state and reconciliation output. Show the provider name and mode on the dashboard. Keep local simulated results and actual sandbox results distinguishable. No integration has been verified merely because an account exists.

## Scoped secret access

Attach the following permission to the approved Devin AWS role and, later, the application runtime role if needed. This policy grants only retrieval of this secret; it does not establish role trust or deployment access.

```json
{
  "Version": "2012-10-17",
  "Statement": [
    {
      "Effect": "Allow",
      "Action": "secretsmanager:GetSecretValue",
      "Resource": "arn:aws:secretsmanager:us-east-1:784620264480:secret:banking-modernization-demo/column-sandbox-IZrTpn"
    }
  ]
}
```
