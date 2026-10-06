# Operator setup

## GitHub and Devin

This public repo contains only original synthetic starter material. In Devin, authorize access to `mortenator/banking-modernization-demo`, start a session with docs/DEVIN_PROMPT.md, and retain the completed session URL. GitHub access by the operator does not establish Devin's repository authorization.

## AWS foundation

The selected account is NatorOS, `784620264480`, in `us-east-1`. The older `serve-sandbox-admin` profile belongs to a different account and must not be used for this demo. A new `natoros` profile is intended for browser-based AWS login. No cloud resources should be described as created until deployment succeeds.

Foundation deployment was verified `CREATE_COMPLETE` on October 6, 2026. Table: `banking-modernization-demo-foundation-DemoTable-1712SBKSQJZXT`. Artifact bucket: `banking-modernization-demo-foundation-artifacts-uqadfnsiynfs`. The modern application is not deployed yet. The operator's login is root; those credentials must remain local and must not be supplied to Devin. A separate scoped integration is still needed for direct Devin deployment.

Authenticate locally, confirm that the resulting account is the intended sandbox, then run:

```sh
aws login --profile natoros --region us-east-1
aws sts get-caller-identity --profile natoros
bash scripts/aws-setup.sh natoros 784620264480 us-east-1
```

The script checks the account before mutation, validates the CloudFormation template and deploys `banking-modernization-demo-foundation`. Outputs are saved to ignored `output/aws-foundation.json`. These resource names and account ID are not secrets; credentials are.

Foundation resources are an on-demand DynamoDB table and a private encrypted S3 artifact bucket. The modern application is a separate deliverable for Devin. It should either reference the foundation table or declare its own table; document which is used. The foundation is deliberately not an AWS credential delegation mechanism.

Devin needs a supported AWS integration with scoped access to deploy directly. Do not give it the operator's admin SSO token. If such integration is unavailable, Devin can finish locally and provide infrastructure for the operator to deploy and verify before the interview. This is a remaining deployment prerequisite, not evidence of an existing cloud demo.

Use pay-per-request/serverless resources, explicit log retention and project tags. Usage can incur charges; no zero-cost guarantee is made. No NAT, RDS, EKS or other always-on infrastructure is required.

## Optional Column sandbox

The mandatory provider simulator needs no account. A real Column sandbox requires sandbox access and credentials, verified current API/webhook specifications, and secure secret provisioning. Keep it optional. Do not paste keys into Devin chat, commit them, or run real-money operations.

To store a dashboard-issued sandbox API key securely in the selected account, run `python3 scripts/store-column-key.py` in a local terminal. Input is hidden; the script accepts only a sandbox-prefixed key, checks the AWS account, and creates a project-tagged Secrets Manager secret. It does not overwrite an existing secret. Store format is JSON with an `api_key` field. The script outputs only the secret ARN. This does not automatically grant Devin access or verify Column authentication. Secrets Manager has its own charges. Delete this project secret during cleanup when it is no longer needed.

## Cleanup

Delete the modern application stack using its own runbook. For the foundation, first inspect its outputs and empty only the artifact bucket identified by that stack if necessary; then delete the exact stack:

```sh
aws cloudformation delete-stack --stack-name banking-modernization-demo-foundation --profile natoros --region us-east-1
aws cloudformation wait stack-delete-complete --stack-name banking-modernization-demo-foundation --profile natoros --region us-east-1
```

Do not delete unrelated buckets, tables or shared resources. DynamoDB demo data is deleted with the foundation stack.
