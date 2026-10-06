"""Run a fresh baseline, verify golden values, and save a reviewable report."""
import json
import tempfile
from pathlib import Path
from legacy.batch import connect, seed, run_batch

root = Path(__file__).resolve().parents[1]
with tempfile.TemporaryDirectory() as tmp:
    with connect(Path(tmp)/"baseline.sqlite") as conn:
        seed(conn)
        actual = run_batch(conn)
expected = json.loads((root/"fixtures/golden_batch.json").read_text())
if actual != expected:
    raise SystemExit("Baseline does not match golden values")
(root/"output").mkdir(exist_ok=True)
(root/"output/baseline.json").write_text(json.dumps(actual,indent=2)+"\n")
print("PASS: baseline matches golden values; report at output/baseline.json")
