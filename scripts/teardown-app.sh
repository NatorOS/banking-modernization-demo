#!/usr/bin/env bash
# Remove the modern application stack only. The foundation table and bucket are preserved unless
# --delete-foundation is passed together with the confirmed account ID.
set -euo pipefail
usage="Usage: bash scripts/teardown-app.sh PROFILE EXPECTED_ACCOUNT_ID [REGION] [--purge-data] [--delete-foundation]"
profile="${1:?$usage}"
expected_account="${2:?$usage}"
region="us-east-1"
if [[ "${3:-}" != "" && "${3:0:2}" != "--" ]]; then region="$3"; shift; fi
shift 2
purge=false; delete_foundation=false
for arg in "$@"; do
  case "$arg" in
    --purge-data) purge=true ;;
    --delete-foundation) delete_foundation=true ;;
    *) echo "$usage" >&2; exit 1 ;;
  esac
done
foundation_stack="${FOUNDATION_STACK:-banking-modernization-demo-foundation}"
app_stack="${APP_STACK:-banking-modernization-demo-app}"

actual_account="$(aws sts get-caller-identity --profile "$profile" --query Account --output text)"
if [[ "$actual_account" != "$expected_account" ]]; then
  echo "Account mismatch; nothing deleted." >&2; exit 1
fi

if $purge; then
  api_url="$(aws cloudformation describe-stacks --stack-name "$app_stack" --profile "$profile" --region "$region" \
    --query "Stacks[0].Outputs[?OutputKey=='ApiUrl'].OutputValue" --output text)"
  for ns in demo-aws demo-aws-edge; do
    python3 -m modern.demo purge --remote "$api_url" --profile "$profile" --region "$region" --namespace "$ns" || true
  done
fi

aws cloudformation delete-stack --stack-name "$app_stack" --profile "$profile" --region "$region"
aws cloudformation wait stack-delete-complete --stack-name "$app_stack" --profile "$profile" --region "$region"
bucket="$(aws cloudformation describe-stacks --stack-name "$foundation_stack" --profile "$profile" --region "$region" \
  --query "Stacks[0].Outputs[?OutputKey=='ArtifactsBucket'].OutputValue" --output text)"
aws s3 rm "s3://$bucket/app/" --recursive --profile "$profile" --region "$region" --only-show-errors
echo "Deleted $app_stack and its Lambda packages under s3://$bucket/app/. Foundation preserved."

if $delete_foundation; then
  read -r -p "Type the account ID to delete foundation stack $foundation_stack: " confirm
  if [[ "$confirm" != "$expected_account" ]]; then echo "Not confirmed; foundation kept." >&2; exit 1; fi
  aws s3 rm "s3://$bucket" --recursive --profile "$profile" --region "$region" --only-show-errors
  aws cloudformation delete-stack --stack-name "$foundation_stack" --profile "$profile" --region "$region"
  aws cloudformation wait stack-delete-complete --stack-name "$foundation_stack" --profile "$profile" --region "$region"
  echo "Foundation deleted."
fi
