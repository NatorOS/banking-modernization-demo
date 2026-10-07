"""Vercel dashboard proxy (api/proxy.py) and its IAM template; no network or AWS calls."""
import importlib.util
import json
import os
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location("vercel_proxy", ROOT / "api" / "proxy.py")
proxy = importlib.util.module_from_spec(spec)
spec.loader.exec_module(proxy)

try:
    import botocore  # noqa: F401
    HAS_BOTOCORE = True
except ImportError:
    HAS_BOTOCORE = False

ENV = {"DEMO_API_URL": "https://abcde12345.execute-api.us-east-1.amazonaws.com",
       "AWS_ROLE_ARN": "arn:aws:iam::123456789012:role/dashboard", "DEMO_AWS_REGION": "us-east-1"}
JSON = {"content-type": "application/json", "x-vercel-oidc-token": "oidc-token"}


class FakeSts:
    def __init__(self):
        self.calls = []

    def assume_role_with_web_identity(self, **kwargs):
        self.calls.append(kwargs)
        return {"Credentials": {"AccessKeyId": "ASIAEXAMPLE", "SecretAccessKey": "secret", "SessionToken": "session",
                                "Expiration": datetime.now(timezone.utc) + timedelta(minutes=15)}}


class RoutingTests(unittest.TestCase):
    def test_resolves_rewritten_and_original_paths(self):
        self.assertEqual(proxy.resolve("/api/proxy?path=namespaces/demo-aws/reconciliation&stage=after_return"),
                         ("/api/namespaces/demo-aws/reconciliation", "stage=after_return"))
        self.assertEqual(proxy.resolve("/api/proxy?path=namespaces%2Fdemo-aws%2Fstate"), ("/api/namespaces/demo-aws/state", ""))
        self.assertEqual(proxy.resolve("/api/namespaces/demo-aws/state"), ("/api/namespaces/demo-aws/state", ""))

    def test_only_dashboard_routes_are_forwarded(self):
        for method, path in [("GET", "/api/health"), ("GET", "/api/legacy/baseline"),
                             ("GET", "/api/namespaces/demo-aws/state"), ("GET", "/api/namespaces/demo-aws/reconciliation"),
                             ("POST", "/api/namespaces/demo-aws/reset"), ("POST", "/api/namespaces/demo-aws/reconcile-pending"),
                             ("POST", "/api/namespaces/demo-aws/demo/provider-event")]:
            self.assertTrue(proxy.allowed(method, path), (method, path))
        for method, path in [("POST", "/api/namespaces/demo-aws/purge"), ("POST", "/api/namespaces/demo-aws/payments"),
                             ("POST", "/api/namespaces/demo-aws/events"), ("GET", "/api/namespaces/demo-aws/reset"),
                             ("GET", "/api/namespaces/prod/state"), ("GET", "/api/namespaces/demo-aws/../x/state"),
                             ("GET", "/api/proxy"), ("GET", "/api/namespaces/demo-aws/state/extra")]:
            self.assertFalse(proxy.allowed(method, path), (method, path))

    def test_rejections_happen_before_credentials(self):
        with mock.patch.dict(os.environ, ENV), mock.patch.object(proxy, "credentials_for") as creds:
            self.assertEqual(proxy.proxy("POST", "/api/proxy?path=namespaces/demo-aws/purge", JSON, b"{}")[0], 404)
            self.assertEqual(proxy.proxy("POST", "/api/proxy?path=namespaces/demo-aws/reset",
                                         {"content-type": "text/plain", "x-vercel-oidc-token": "t"}, b"{}")[0], 415)
            creds.assert_not_called()

    def test_reports_missing_configuration(self):
        with mock.patch.dict(os.environ, {}, clear=True):
            status, body = proxy.proxy("GET", "/api/proxy?path=health", {}, b"")
            self.assertEqual((status, json.loads(body)["error"]["code"]), (500, "not_configured"))
        with mock.patch.dict(os.environ, ENV, clear=True):
            status, body = proxy.proxy("GET", "/api/proxy?path=health", {}, b"")
            self.assertEqual((status, json.loads(body)["error"]["code"]), (500, "no_oidc_token"))

    def test_sts_failure_is_a_502_without_details(self):
        with mock.patch.dict(os.environ, ENV), mock.patch.object(proxy, "credentials_for", side_effect=RuntimeError("x")):
            status, body = proxy.proxy("GET", "/api/proxy?path=health", JSON, b"")
        self.assertEqual((status, json.loads(body)["error"]["code"]), (502, "credentials_unavailable"))


@unittest.skipUnless(HAS_BOTOCORE, "botocore not installed")
class SigningTests(unittest.TestCase):
    def test_assumed_role_credentials_are_cached(self):
        sts = FakeSts()
        creds = proxy.RoleCredentials(ENV["AWS_ROLE_ARN"], "us-east-1", sts=sts)
        first, second = creds.get("oidc-token"), creds.get("oidc-token")
        self.assertIs(first, second)
        self.assertEqual(len(sts.calls), 1)
        self.assertEqual(sts.calls[0]["RoleArn"], ENV["AWS_ROLE_ARN"])
        self.assertEqual(sts.calls[0]["WebIdentityToken"], "oidc-token")

    def test_forwards_a_sigv4_signed_request(self):
        credentials = proxy.RoleCredentials(ENV["AWS_ROLE_ARN"], "us-east-1", sts=FakeSts()).get("t")
        sent = []
        with mock.patch.dict(os.environ, ENV), mock.patch.object(proxy, "credentials_for", return_value=credentials), \
                mock.patch.object(proxy, "send", side_effect=lambda r: sent.append(r) or (200, b'{"ok":true}')):
            status, _ = proxy.proxy("POST", "/api/proxy?path=namespaces/demo-aws/demo/provider-event",
                                    {**JSON, "idempotency-key": "k1", "cookie": "_vercel_jwt=x"}, b'{"payment_id":"PAY-002"}')
        self.assertEqual(status, 200)
        request = sent[0]
        headers = {k.lower(): v for k, v in request.header_items()}
        self.assertEqual(request.full_url, ENV["DEMO_API_URL"] + "/api/namespaces/demo-aws/demo/provider-event")
        self.assertIn("/us-east-1/execute-api/aws4_request", headers["authorization"])
        self.assertEqual(headers["x-amz-security-token"], "session")
        self.assertEqual(headers["idempotency-key"], "k1")
        self.assertNotIn("cookie", headers)
        self.assertNotIn("x-vercel-oidc-token", headers)

    def test_rejects_non_https_api_url(self):
        credentials = proxy.RoleCredentials(ENV["AWS_ROLE_ARN"], "us-east-1", sts=FakeSts()).get("t")
        with self.assertRaises(ValueError):
            proxy.signed_request("http://example.com", "us-east-1", credentials, "GET", "/api/health", "", b"", {})


class VercelConfigTests(unittest.TestCase):
    def setUp(self):
        self.template = json.loads((ROOT / "infra" / "vercel-access.json").read_text())
        self.config = json.loads((ROOT / "vercel.json").read_text())

    def test_role_trusts_production_of_one_project_only(self):
        trust = self.template["Resources"]["DashboardProxyRole"]["Properties"]["AssumeRolePolicyDocument"]["Fn::Sub"][0]
        self.assertIn('"oidc.vercel.com/${TeamSlug}:sub": "owner:${TeamSlug}:project:${ProjectName}:environment:production"', trust)
        self.assertIn('"oidc.vercel.com/${TeamSlug}:aud": "https://vercel.com/${TeamSlug}"', trust)
        self.assertNotIn("StringLike", trust)
        json.loads(trust)  # the substituted document stays valid JSON

    def test_role_can_only_invoke_dashboard_routes(self):
        [policy] = self.template["Resources"]["DashboardProxyRole"]["Properties"]["Policies"]
        [statement] = policy["PolicyDocument"]["Statement"]
        self.assertEqual(statement["Action"], "execute-api:Invoke")
        resources = " ".join(r["Fn::Sub"] for r in statement["Resource"])
        for blocked in ("purge", "payments", "events", "/*/*/"):
            self.assertNotIn(blocked, resources)
        self.assertNotIn("dynamodb", json.dumps(self.template))

    def test_proxy_is_outside_the_lambda_package(self):
        from scripts import package_lambda
        self.assertNotIn("api", {directory for directory, _ in package_lambda.INCLUDE})
        self.assertEqual(self.config["rewrites"], [{"source": "/api/:path*", "destination": "/api/proxy?path=:path*"}])
        self.assertEqual(self.config["outputDirectory"], "web/dist")


if __name__ == "__main__":
    unittest.main()
