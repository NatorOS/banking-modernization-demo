"""Verify IAM authentication and concurrent payments through the deployed API/Lambda.

Fresh synthetic namespaces only; the demonstration namespace is left untouched.
"""
import argparse
import json
import sys
import time
import urllib.error
import urllib.request
import uuid
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--profile", required=True)
    parser.add_argument("--expected-account", required=True)
    parser.add_argument("--region", default="us-east-1", choices=["us-east-1"])
    parser.add_argument("--stack", default="banking-modernization-demo-app")
    parser.add_argument("--output", default="output/aws-api-verification.json")
    args = parser.parse_args()
    import boto3
    from modern.aws_client import SignedApiClient
    session = boto3.Session(profile_name=args.profile, region_name=args.region)
    account = session.client("sts").get_caller_identity()["Account"]
    if account != args.expected_account:
        raise SystemExit("account mismatch; no API mutation attempted")
    stack = session.client("cloudformation").describe_stacks(StackName=args.stack)["Stacks"][0]
    url = next(o["OutputValue"] for o in stack["Outputs"] if o["OutputKey"] == "ApiUrl")
    client = SignedApiClient(url, args.profile, args.region)
    namespace = "demo-api-" + uuid.uuid4().hex[:20]
    base = "/api/namespaces/" + namespace
    report = {"tested_at": datetime.now(timezone.utc).isoformat(), "account": account,
              "region": args.region, "api_url": url, "namespace": namespace, "checks": {}}

    def request(method, path, data=None, headers=None):
        for attempt in range(6):
            status, body = client.request(method, path, data, headers)
            if status != 429:
                return status, body
            time.sleep(0.2 * (2 ** attempt))
        raise RuntimeError("API Gateway throttling did not clear")

    def submit(index, same_key):
        payment = {"payment_id": "PAY-001" if same_key else f"PAY-{100 + index}",
                   "beneficiary": "Synthetic API concurrency test", "amount_cents": 400,
                   "currency": "USD"}
        key = "live-api-same-key" if same_key else f"live-api-spend-{index:04d}"
        return request("POST", base + "/payments", payment, {"Idempotency-Key": key})

    try:
        try:
            with urllib.request.urlopen(url + "/api/health", timeout=30) as response:
                unsigned = response.status
        except urllib.error.HTTPError as exc:
            unsigned = exc.code
        assert unsigned == 403, f"unsigned request returned {unsigned}"
        report["checks"]["unsigned_request"] = unsigned
        status, health = request("GET", "/api/health")
        assert status == 200 and health["environment"] == "aws" and health["store"] == "dynamodb"
        report["checks"]["signed_health"] = health

        assert request("POST", base + "/reset", {"opening_cash_cents": 1000})[0] == 200
        with ThreadPoolExecutor(max_workers=8) as pool:
            same = list(pool.map(lambda i: submit(i, True), range(8)))
        assert all(status in (200, 201, 202) for status, _ in same), same
        assert request("POST", base + "/reconcile-pending", {})[0] == 200
        status, state = request("GET", base + "/state")
        assert status == 200 and len(state["snapshot"]["payments"]) == 1
        assert state["snapshot"]["balance"]["reserved_cents"] == 400
        assert state["reconciliation"]["status"] == "PASS"
        report["checks"]["same_key"] = {"responses": [s for s, _ in same], "payment_count": 1,
                                        "reserved_cents": 400, "reconciliation": "PASS"}

        assert request("POST", base + "/reset", {"opening_cash_cents": 1000})[0] == 200
        with ThreadPoolExecutor(max_workers=8) as pool:
            spend = list(pool.map(lambda i: submit(i, False), range(8)))
        assert sorted(s for s, _ in spend) == [201, 201] + [422] * 6, spend
        status, state = request("GET", base + "/state")
        balance = state["snapshot"]["balance"]
        assert status == 200 and len(state["snapshot"]["payments"]) == 2
        assert (balance["reserved_cents"], balance["available_cents"]) == (800, 200)
        assert state["reconciliation"]["status"] == "PASS"
        report["checks"]["competing_spend"] = {"responses": [s for s, _ in spend],
                                               "payment_count": 2, "balance": balance,
                                               "reconciliation": "PASS"}
        report["success"] = True
    finally:
        status, _ = request("POST", base + "/purge", {"confirm": namespace})
        report["cleanup_http_status"] = status
        report["cleanup"] = "test namespace purged; generation tombstone preserved"
        path = Path(args.output)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(report, indent=2) + "\n")
    print(f"AWS API authentication and concurrency PASS; evidence at {args.output}")


if __name__ == "__main__":
    main()
