#!/usr/bin/env bash
# Deploy the modern application stack onto the existing foundation. Run by the operator with
# their own scoped AWS profile; this script never reads or prints credentials or secret values.
set -euo pipefail
usage="Usage: bash scripts/deploy-app.sh PROFILE EXPECTED_ACCOUNT_ID [REGION]"
profile="${1:?$usage}"
expected_account="${2:?$usage}"
region="${3:-us-east-1}"
expected_region="${EXPECTED_REGION:-us-east-1}"
foundation_stack="${FOUNDATION_STACK:-banking-modernization-demo-foundation}"
app_stack="${APP_STACK:-banking-modernization-demo-app}"

if [[ "$region" != "$expected_region" ]]; then
  echo "Region $region does not match expected $expected_region; nothing deployed." >&2; exit 1
fi
actual_account="$(aws sts get-caller-identity --profile "$profile" --query Account --output text)"
if [[ "$actual_account" != "$expected_account" ]]; then
  echo "Account mismatch (profile resolves to a different account); nothing deployed." >&2; exit 1
fi

output() {
  aws cloudformation describe-stacks --stack-name "$foundation_stack" --profile "$profile" --region "$region" \
    --query "Stacks[0].Outputs[?OutputKey=='$1'].OutputValue" --output text
}
table="$(output TableName)"
bucket="$(output ArtifactsBucket)"
if [[ -z "$table" || "$table" == "None" || -z "$bucket" || "$bucket" == "None" ]]; then
  echo "Foundation stack $foundation_stack outputs not found; deploy the foundation first." >&2; exit 1
fi
echo "Account $actual_account, region $region, table $table, bucket $bucket"

python3 scripts/package_lambda.py output/lambda.zip
digest="$(python3 -c 'import hashlib,sys;print(hashlib.sha256(open(sys.argv[1],"rb").read()).hexdigest()[:16])' output/lambda.zip)"
key="app/lambda-$digest.zip"
aws s3 cp output/lambda.zip "s3://$bucket/$key" --profile "$profile" --region "$region" --only-show-errors

aws cloudformation validate-template --template-body file://infra/app.json --profile "$profile" --region "$region" >/dev/null
aws cloudformation deploy --template-file infra/app.json --stack-name "$app_stack" \
  --parameter-overrides "TableName=$table" "ArtifactsBucket=$bucket" "CodeKey=$key" \
  --capabilities CAPABILITY_IAM --profile "$profile" --region "$region" \
  --tags Project=banking-modernization-demo DataClassification=synthetic --no-fail-on-empty-changeset

mkdir -p output
aws cloudformation describe-stacks --stack-name "$app_stack" --profile "$profile" --region "$region" \
  --query 'Stacks[0].Outputs' --output json > output/aws-app.json
api_url="$(aws cloudformation describe-stacks --stack-name "$app_stack" --profile "$profile" --region "$region" \
  --query "Stacks[0].Outputs[?OutputKey=='ApiUrl'].OutputValue" --output text)"
echo "Deployed $app_stack. API (IAM/SigV4 required): $api_url"
echo "Unsigned request should be rejected (expect 403):"
curl -s -o /dev/null -w '%{http_code}\n' "$api_url/api/health" || true
echo "Next: python3 -m modern.demo run --remote $api_url --profile $profile"
