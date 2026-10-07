# Hosting the dashboard on Vercel

Optional. The local launcher (`python3 -m scripts.dashboard --remote ...`) keeps working unchanged.

```
browser ── Vercel Authentication ──▶ Vercel (static Arc build, web/dist)
                                        │  /api/*  (rewrite)
                                        ▼
                                  api/proxy.py  ── OIDC token ──▶ AWS STS (AssumeRoleWithWebIdentity)
                                        │  SigV4, short-lived role credentials
                                        ▼
                     API Gateway HTTP API (IAM auth) ─▶ Lambda ─▶ DynamoDB   (unchanged)
```

What is real and what is simulated is the same as before: API Gateway, IAM, Lambda and DynamoDB are
real; the payment provider is the in-Lambda simulator.

## Security model

- **No stored AWS keys.** Vercel issues the function an OIDC token per request; the function exchanges
  it for 15-minute credentials of `DashboardProxyRole` ([infra/vercel-access.json](../infra/vercel-access.json)).
- **Production only.** The role's trust policy matches `sub` =
  `owner:<team>:project:<project>:environment:production` exactly. Preview deployments get
  `502 credentials_unavailable` by design.
- **Dashboard routes only.** The proxy and the role allow `health`, `legacy/baseline`, `state`,
  `reconciliation`, `reset`, `reconcile-pending` and `demo/*` in `demo-*` namespaces. `purge`, raw
  `payments` and `events` are not reachable. Cookies and the OIDC token are never forwarded.
- **Login required.** Set Deployment Protection to Vercel Authentication with scope **All Deployments**
  (available on all plans) so the production URL is not public. POSTs must be `application/json`, so a
  cross-site form cannot drive the demo.
- The Lambda package is unchanged (`api/`, `web/` and `scripts/` are outside it).

## Setup

1. **Create the Vercel project** from this repository (project root = repository root). `vercel.json`
   sets the install/build commands, output directory, the `/api/*` rewrite and region `iad1`. In
   Project Settings → Security, confirm OIDC federation is enabled with issuer mode **Team**.
2. **Deploy the role** (operator, own AWS profile; checks account and region first):
   ```bash
   bash scripts/deploy-vercel-access.sh natoros 784620264480 <vercel-team-slug> <vercel-project-name>
   ```
   It prints the role ARN. If `oidc.vercel.com/<team>` already exists as an IAM identity provider, set
   `EXISTING_OIDC_PROVIDER_ARN=<its ARN>` first.
3. **Set Production environment variables** on the Vercel project:
   - `AWS_ROLE_ARN` = the printed role ARN
   - `DEMO_API_URL` = `https://15ggin9fcd.execute-api.us-east-1.amazonaws.com`
   - `DEMO_AWS_REGION` = `us-east-1`
   - optional `DEMO_NAMESPACE` (default `demo-aws`)
4. **Enable Deployment Protection**: Vercel Authentication, All Deployments.
5. **Deploy**: `vercel --prod` (or push to the production branch).

## Verify

- Signed out, the production URL redirects to the Vercel login.
- Signed in, the header shows `AWS (Vercel SigV4 proxy to 15ggin9fcd...)`; run 1 → 6b as in the
  [runbook](RUNBOOK.md). Expected end state: $850 cash, $220 debits/credits, PASS 14/14.
- `curl -X POST https://<deployment>/api/namespaces/demo-aws/purge` (with a bypass or session) returns
  `404 not_proxied`; unsigned calls straight to the API Gateway URL still return `403`.

## Remove

Delete the Vercel project and the `banking-modernization-demo-vercel-access` stack. The app and
foundation stacks are unaffected.
