#!/usr/bin/env bash
set -euo pipefail
profile="${1:?Usage: bash scripts/aws-setup.sh PROFILE CONFIRMED_ACCOUNT_ID [REGION]}"
expected_account="${2:?Supply the intended AWS account ID}"
region="${3:-us-east-1}"
actual_account="$(aws sts get-caller-identity --profile "$profile" --query Account --output text)"
if [[ "$actual_account" != "$expected_account" ]]; then
  echo "Account mismatch; no resources created." >&2
  exit 1
fi
aws cloudformation validate-template --template-body file://infra/foundation.json --profile "$profile" --region "$region" >/dev/null
aws cloudformation deploy --template-file infra/foundation.json --stack-name banking-modernization-demo-foundation --profile "$profile" --region "$region" --tags Project=banking-modernization-demo DataClassification=synthetic --no-fail-on-empty-changeset
mkdir -p output
aws cloudformation describe-stacks --stack-name banking-modernization-demo-foundation --profile "$profile" --region "$region" --query 'Stacks[0].Outputs' --output json > output/aws-foundation.json
echo "Foundation deployed to account $actual_account in $region. Outputs: output/aws-foundation.json"
