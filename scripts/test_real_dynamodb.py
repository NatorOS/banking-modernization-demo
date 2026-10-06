"""Run the shared behavioral suite on real DynamoDB, without moto or a request lock.

Requires explicit account/table/profile arguments. Every test gets fresh, random synthetic
partitions; cleanup deletes only those partitions, including generation tombstones. Test
namespaces are never reused. No table, credentials, or external-provider secret is created.
"""
import argparse
import importlib.util
import json
import os
import sys
import time
import unittest
import uuid
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))


def load_behavior(namespace):
    name = "live_behavior_" + uuid.uuid4().hex
    spec = importlib.util.spec_from_file_location(name, ROOT / "tests/test_modern.py")
    module = importlib.util.module_from_spec(spec)
    previous = os.environ.get("MODERN_TEST_NAMESPACE")
    os.environ["MODERN_TEST_NAMESPACE"] = namespace
    try:
        spec.loader.exec_module(module)
    finally:
        if previous is None:
            os.environ.pop("MODERN_TEST_NAMESPACE", None)
        else:
            os.environ["MODERN_TEST_NAMESPACE"] = previous
    return module


def live_case(module, client, table, method):
    from modern.store import DynamoStore

    class LiveDynamoTests(module.BehaviorSuite, unittest.TestCase):
        def make_store(self):
            store = DynamoStore(table, client=client)
            self.addCleanup(self.cleanup_partitions, store)
            return store

        def cleanup_partitions(self, store):
            for namespace in (module.NS, module.OTHER_NS):
                # These names were generated for this test invocation, never user supplied.
                if not namespace.startswith("demo-live-"):
                    raise RuntimeError("refusing cleanup outside the generated test partitions")
                for prefix in ("RUN#", "SIM#", "META"):
                    store.delete_prefix("NS#" + namespace, prefix)

        def build(self, store):
            super().build(store)
            # Real network transactions need backoff under contention, unlike injected mocks.
            self.service.sleep = time.sleep

        def reopen_store(self):
            self.store = DynamoStore(table, client=client)
            return self.store

    return LiveDynamoTests(method)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--profile", required=True)
    parser.add_argument("--table", required=True)
    parser.add_argument("--expected-account", required=True)
    parser.add_argument("--region", default="us-east-1", choices=["us-east-1"])
    parser.add_argument("--repeat", type=int, default=1)
    parser.add_argument("--filter", default="", help="optional substring of a test name")
    parser.add_argument("--output", default="output/real-dynamodb-tests.json")
    args = parser.parse_args()
    if not 1 <= args.repeat <= 10:
        parser.error("repeat must be 1..10")
    import boto3
    session = boto3.Session(profile_name=args.profile, region_name=args.region)
    account = session.client("sts").get_caller_identity()["Account"]
    if account != args.expected_account:
        raise SystemExit("AWS account mismatch; no test data written")
    client = session.client("dynamodb")
    table = client.describe_table(TableName=args.table)["Table"]
    expected_arn = f"arn:aws:dynamodb:{args.region}:{account}:table/{args.table}"
    if table["TableArn"] != expected_arn or table["TableStatus"] != "ACTIVE":
        raise SystemExit("table identity/status mismatch; no test data written")
    key_schema = {k["AttributeName"]: k["KeyType"] for k in table["KeySchema"]}
    if key_schema != {"pk": "HASH", "sk": "RANGE"}:
        raise SystemExit("table schema mismatch; no test data written")

    methods = unittest.defaultTestLoader.getTestCaseNames(
        type("DiscoverBehavior", (load_behavior("demo-live-discover").BehaviorSuite, unittest.TestCase), {}))
    methods = [method for method in methods if args.filter in method]
    if not methods:
        parser.error("filter matched no tests")
    suite, namespaces = unittest.TestSuite(), []
    for _ in range(args.repeat):
        for method in methods:
            namespace = "demo-live-" + uuid.uuid4().hex[:20]
            module = load_behavior(namespace)
            namespaces.append(namespace)
            suite.addTest(live_case(module, client, args.table, method))
    started = time.monotonic()
    result = unittest.TextTestRunner(verbosity=2).run(suite)
    report = {
        "tested_at": datetime.now(timezone.utc).isoformat(),
        "backend": "real-dynamodb", "moto": False, "request_lock": False,
        "account": account, "region": args.region, "table": args.table,
        "tests_run": result.testsRun, "repeat": args.repeat,
        "duration_seconds": round(time.monotonic() - started, 2),
        "failures": [{"test": str(t), "traceback": trace} for t, trace in result.failures],
        "errors": [{"test": str(t), "traceback": trace} for t, trace in result.errors],
        "namespaces": namespaces, "cleanup": "generated test partitions only",
        "success": result.wasSuccessful(),
    }
    path = Path(args.output)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(report, indent=2) + "\n")
    print(f"Evidence written to {path}")
    return 0 if result.wasSuccessful() else 1


if __name__ == "__main__":
    raise SystemExit(main())
