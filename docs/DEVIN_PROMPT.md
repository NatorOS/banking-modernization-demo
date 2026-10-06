# Devin task: modernize a synthetic bank payment integration on AWS

Repository: https://github.com/mortenator/banking-modernization-demo

I am preparing a banking modernization demonstration for a Cognition product partnerships interview. Build the modernization in this repo and open a PR. This is an integration and reconciliation case around a modern banking core, not a full core replacement.

First read README.md, docs/ACCEPTANCE.md, docs/SETUP.md, legacy/batch.py, the tests and fixtures. Run the baseline before editing. Preserve the original legacy code, baseline tests and golden fixture. Explain your proposed state machine and ledger semantics, then implement the solution.

Build an event-driven payment integration with:

- A provider interface and deterministic simulated payment provider.
- Durable payment submission idempotency, provider settlement and return events, duplicate handling, out-of-order handling, and balanced immutable journal entries.
- A minimal dashboard and repeatable demo showing the original batch outcome, equivalent modern outcome, duplicate settlement, a return, and duplicate return.
- A reconciliation report that verifies balances and statuses against expected results.
- A fully runnable local mode and deployable AWS mode using API Gateway HTTP API, Python Lambda and on-demand DynamoDB. Keep dependencies modest. Use atomic writes and conditional updates in DynamoDB for correctness under retries and concurrency.

The detailed required scenarios and exact monetary expectations are in docs/ACCEPTANCE.md. Cover every one in meaningful tests. Add CI for modern tests while retaining the original checks. Do not change expected values to make tests pass.

AWS foundation resources may already exist. The operator can provide their non-secret names, region and account through the setup output. If they are absent, proceed with local development and supply deployment infrastructure. Never invent a successful deployment. Do not request or embed AWS admin credentials. Use an approved integration with scoped credentials if one exists; otherwise provide an executable deployment command for the operator. Never include secrets in code, logs, PRs or session text.

Use the simulator for the mandatory demo. Column sandbox support is optional and must not block delivery. If credentials and permission are available through a secure integration, verify current Column API/webhook documentation and add a clearly labeled sandbox adapter. Do not claim a real integration with any other core provider.

Add these deliverables to the PR:

1. Modern implementation, dashboard, tests and infrastructure.
2. A five-minute demo runbook, including commands and expected results, and a reliable reset for synthetic data only.
3. An architecture diagram and brief explanation of the partner roles: bank, implementation consultancy, core provider, Cognition and AWS.
4. A consumption explanation naming the AWS services and what drives their usage; separate measurements from estimates and make no unsupported Marketplace or pricing claims.
5. Security/deployment notes covering credentials, webhook authenticity, network boundaries, data egress, logging, and what would still be required for a real bank.
6. A concise PR summary of what was built, actual checks run, evidence of correctness, deployment status and limitations.

Deploy and exercise the AWS endpoint only if an authorized AWS integration is available. Otherwise finish the local demonstration and deployment package and explicitly mark AWS deployment pending. Do not provision always-on databases, NAT gateways, Kubernetes, or unrelated resources. Do not contact third parties. Finish with a PR URL and exact steps for me to run the demo.
