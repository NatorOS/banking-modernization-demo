"""Build the application from environment configuration (local SQLite or AWS DynamoDB)."""
import json
import os
import secrets
from pathlib import Path

from .api import App
from .provider import SimulatedProvider
from .service import PaymentService
from .store import DynamoStore, SqliteStore
from .webhook import WebhookSigner


def local_webhook_key(data_dir):
    """Per-checkout simulator signing key, generated locally and never committed (data/ is ignored)."""
    env_key = os.environ.get("WEBHOOK_HMAC_KEY")
    if env_key:
        return env_key
    path = Path(data_dir) / "webhook.key"
    if not path.exists():
        path.parent.mkdir(parents=True, exist_ok=True)
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(fd, "w") as handle:
            handle.write(secrets.token_hex(32))
    return path.read_text().strip()


def secrets_manager_webhook_key(secret_arn):
    import boto3
    value = boto3.client("secretsmanager").get_secret_value(SecretId=secret_arn)["SecretString"]
    return json.loads(value)["hmac_key"]


def build_app(env=None, sqlite_path=None):
    env = os.environ if env is None else env
    provider_name = env.get("PAYMENT_PROVIDER", "simulator")
    if provider_name != "simulator":
        raise SystemExit("Only PAYMENT_PROVIDER=simulator is implemented; no live provider adapter exists.")
    backend = env.get("STORE_BACKEND", "sqlite")
    if backend == "dynamodb":
        store = DynamoStore(env["TABLE_NAME"])
        key = secrets_manager_webhook_key(env["WEBHOOK_SECRET_ARN"])
        environment = "aws"
    elif backend == "sqlite":
        path = sqlite_path or env.get("SQLITE_PATH", "data/modern.sqlite")
        store = SqliteStore(path)
        key = local_webhook_key(Path(path).parent)
        environment = "local"
    else:
        raise SystemExit(f"Unsupported STORE_BACKEND {backend!r}")
    signer = WebhookSigner(key)
    provider = SimulatedProvider(store, signer)
    return App(PaymentService(store, provider, verifier=signer), provider, environment)
