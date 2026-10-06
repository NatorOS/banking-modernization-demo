"""Store a sandbox key without placing it in argv, history, files or output."""
import getpass
import json
import subprocess


def aws(*args, secret=None):
    return subprocess.run(
        ["aws", *args, "--profile", "natoros", "--region", "us-east-1"],
        input=secret, text=True, capture_output=True, check=True,
    ).stdout.strip()


def main():
    account = aws("sts", "get-caller-identity", "--query", "Account", "--output", "text")
    if account != "784620264480":
        raise SystemExit("Wrong AWS account; nothing stored.")
    key = getpass.getpass("Column sandbox API key (hidden): ").strip()
    if not key.startswith("test_"):
        raise SystemExit("Expected a Column sandbox key beginning test_; nothing stored.")
    # Create-only: never overwrite an existing secret without operator review.
    arn = aws(
        "secretsmanager", "create-secret", "--name", "banking-modernization-demo/column-sandbox",
        "--description", "Column sandbox API key for synthetic banking demo",
        "--secret-string", "file:///dev/stdin", "--tags", "Key=Project,Value=banking-modernization-demo",
        "--query", "ARN", "--output", "text", secret=json.dumps({"api_key": key}),
    )
    print("Stored sandbox key. Secret ARN (safe to share):", arn)
    print("Sandbox authentication has not yet been verified.")


if __name__ == "__main__":
    try:
        main()
    except subprocess.CalledProcessError:
        raise SystemExit("AWS operation failed; no credential output was printed. Check login and whether the secret already exists.")
