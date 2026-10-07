#!/usr/bin/env bash
# Optional: deploy the IAM role the Vercel-hosted dashboard assumes through Vercel OIDC.
# Run by the operator with their own AWS profile; never reads or prints credentials.
set -euo pipefail
usage="Usage: bash scripts/deploy-vercel-access.sh PROFILE EXPECTED_ACCOUNT_ID VERCEL_TEAM_SLUG VERCEL_PROJECT [REGION]"
profile="${1:?$usage}"
expected_account="${2:?$usage}"
team="${3:?$usage}"
project="${4:?$usage}"
region="${5:-us-east-1}"
expected_region="${EXPECTED_REGION:-us-east-1}"
app_stack="${APP_STACK:-banking-modernization-demo-app}"
access_stack="${VERCEL_ACCESS_STACK:-banking-modernization-demo-vercel-access}"

if [[ "$region" != "$expected_region" ]]; then
  echo "Region $region does not match expected $expected_region; nothing deployed." >&2; exit 1
fi
actual_account="$(aws sts get-caller-identity --profile "$profile" --query Account --output text)"
if [[ "$actual_account" != "$expected_account" ]]; then
  echo "Account mismatch (profile resolves to a different account); nothing deployed." >&2; exit 1
fi
api_url="$(aws cloudformation describe-stacks --stack-name "$app_stack" --profile "$profile" --region "$region" \
  --query "Stacks[0].Outputs[?OutputKey=='ApiUrl'].OutputValue" --output text)"
api_id="$(python3 -c 'import sys;from urllib.parse import urlsplit;print(urlsplit(sys.argv[1]).netloc.split(".")[0])' "$api_url")"
if [[ ! "$api_id" =~ ^[a-z0-9]{10}$ ]]; then
  echo "App stack $app_stack has no ApiUrl output; deploy the app stack first." >&2; exit 1
fi
echo "Account $actual_account, region $region, API $api_id, Vercel $team/$project (production only)"

aws cloudformation validate-template --template-body file://infra/vercel-access.json --profile "$profile" --region "$region" >/dev/null
aws cloudformation deploy --template-file infra/vercel-access.json --stack-name "$access_stack" \
  --parameter-overrides "TeamSlug=$team" "ProjectName=$project" "ApiId=$api_id" \
    "ExistingOidcProviderArn=${EXISTING_OIDC_PROVIDER_ARN:-}" \
  --capabilities CAPABILITY_IAM --profile "$profile" --region "$region" --no-fail-on-empty-changeset
aws cloudformation describe-stacks --stack-name "$access_stack" --profile "$profile" --region "$region" \
  --query "Stacks[0].Outputs[?OutputKey=='RoleArn'].OutputValue" --output text
