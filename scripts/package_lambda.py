"""Build a deterministic Lambda zip with only the runtime code (stdlib + boto3 from the runtime)."""
import sys
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
INCLUDE = [("modern", "*.py"), ("modern", "*.html"), ("legacy", "*.py"), ("fixtures", "*.json")]


def build(out):
    out = Path(out)
    out.parent.mkdir(parents=True, exist_ok=True)
    files = sorted({p for d, pattern in INCLUDE for p in (ROOT / d).glob(pattern)})
    with zipfile.ZipFile(out, "w", zipfile.ZIP_DEFLATED) as zf:
        for path in files:
            info = zipfile.ZipInfo(str(path.relative_to(ROOT)), date_time=(2026, 1, 1, 0, 0, 0))
            info.external_attr = 0o644 << 16
            zf.writestr(info, path.read_bytes(), zipfile.ZIP_DEFLATED)
    return out, len(files)


if __name__ == "__main__":
    path, count = build(sys.argv[1] if len(sys.argv) > 1 else "output/lambda.zip")
    print(f"{path} ({count} files)")
