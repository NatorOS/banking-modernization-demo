import json
import tempfile
import unittest
from pathlib import Path
from legacy.batch import connect, seed, run_batch, report

class LegacyTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.conn = connect(Path(self.temp.name)/"legacy.sqlite")
        seed(self.conn)

    def tearDown(self):
        self.conn.close()
        self.temp.cleanup()

    def test_matches_independent_golden_batch(self):
        expected = json.loads((Path(__file__).resolve().parents[1]/"fixtures/golden_batch.json").read_text())
        self.assertEqual(run_batch(self.conn), expected)

    def test_rerun_does_not_double_post(self):
        first = run_batch(self.conn)
        self.assertEqual(run_batch(self.conn), first)
        self.assertEqual(self.conn.execute("SELECT COUNT(*) FROM journal").fetchone()[0],6)

    def test_insufficient_cash_rolls_back_entire_batch(self):
        with self.conn:
            self.conn.execute("UPDATE cash SET cents=100 WHERE id=1")
        before = report(self.conn)
        with self.assertRaises(ValueError):
            run_batch(self.conn)
        self.assertEqual(report(self.conn),before)

    def test_reseeding_does_not_reset_cash(self):
        run_batch(self.conn)
        seed(self.conn)
        self.assertEqual(report(self.conn)["cash_cents"],81500)

if __name__=="__main__":
    unittest.main()
