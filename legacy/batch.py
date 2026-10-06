"""Synthetic legacy batch processor. This is not a production bank ledger."""
import argparse
import json
import sqlite3
from pathlib import Path

FIXTURE = Path(__file__).resolve().parents[1] / "fixtures" / "payments.json"

def connect(path):
    Path(path).parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    conn.executescript("""
      CREATE TABLE IF NOT EXISTS cash(id INTEGER PRIMARY KEY CHECK(id=1), cents INTEGER NOT NULL);
      CREATE TABLE IF NOT EXISTS payments(id TEXT PRIMARY KEY, beneficiary TEXT NOT NULL,
        amount_cents INTEGER NOT NULL CHECK(amount_cents>0), status TEXT NOT NULL);
      CREATE TABLE IF NOT EXISTS journal(id INTEGER PRIMARY KEY, payment_id TEXT NOT NULL,
        side TEXT NOT NULL CHECK(side IN ('DEBIT','CREDIT')), account TEXT NOT NULL,
        amount_cents INTEGER NOT NULL, UNIQUE(payment_id,side));
    """)
    return conn

def seed(conn):
    fixture = json.loads(FIXTURE.read_text())
    with conn:
        conn.execute("INSERT OR IGNORE INTO cash VALUES(1,?)", (fixture["opening_cash_cents"],))
        for p in fixture["payments"]:
            conn.execute("INSERT OR IGNORE INTO payments VALUES(?,?,?,'QUEUED')",
                         (p["id"],p["beneficiary"],p["amount_cents"]))

def run_batch(conn):
    # One end-of-day transaction. No external provider, asynchronous events, or return workflow.
    with conn:
        conn.execute("BEGIN IMMEDIATE")
        rows = conn.execute("SELECT * FROM payments WHERE status='QUEUED' ORDER BY id").fetchall()
        total = sum(p["amount_cents"] for p in rows)
        if total > conn.execute("SELECT cents FROM cash WHERE id=1").fetchone()[0]:
            raise ValueError("Insufficient cash for batch")
        for p in rows:
            conn.execute("UPDATE cash SET cents=cents-? WHERE id=1", (p["amount_cents"],))
            conn.execute("UPDATE payments SET status='SETTLED' WHERE id=?", (p["id"],))
            conn.execute("INSERT INTO journal(payment_id,side,account,amount_cents) VALUES(?,?,?,?)",
                         (p["id"],"DEBIT","payment_outflow",p["amount_cents"]))
            conn.execute("INSERT INTO journal(payment_id,side,account,amount_cents) VALUES(?,?,?,?)",
                         (p["id"],"CREDIT","cash",p["amount_cents"]))
    return report(conn)

def report(conn):
    payments = [dict(p) for p in conn.execute("SELECT * FROM payments ORDER BY id")]
    sums = dict(conn.execute("SELECT side,SUM(amount_cents) FROM journal GROUP BY side"))
    return {"currency":"USD", "cash_cents":conn.execute("SELECT cents FROM cash WHERE id=1").fetchone()[0],
            "settled_cents":sum(p["amount_cents"] for p in payments if p["status"]=="SETTLED"),
            "debit_cents":sums.get("DEBIT",0), "credit_cents":sums.get("CREDIT",0),
            "payment_count":len(payments), "statuses":{p["id"]:p["status"] for p in payments}}

def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("command", choices=["seed","run","report"])
    parser.add_argument("--db", default="data/legacy.sqlite")
    args = parser.parse_args()
    with connect(args.db) as conn:
        seed(conn)
        result = run_batch(conn) if args.command=="run" else report(conn)
        print(json.dumps(result,indent=2))

if __name__ == "__main__":
    main()
