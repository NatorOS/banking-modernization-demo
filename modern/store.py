"""Conditional-transaction key/value store with SQLite (local) and DynamoDB (AWS) backends.

Every business write goes through ``transact``: a list of conditional operations that commit
together or not at all. The service never relies on an in-process lock for correctness.

Operations
- ``Put``: insert; fails if the item already exists (insert-only).
- ``Replace``: overwrite; fails unless the stored version equals ``expected_version``.
- ``Increment``: add integer deltas; fails if the item is missing or any field would go negative.
- ``Check``: no write; fails unless ``field`` equals ``value`` (used to pin the active run).

Each item carries ``_v`` (version). A transaction may touch an item at most once, mirroring the
DynamoDB TransactWriteItems rule, so both backends behave identically.
"""
import json
import sqlite3
import threading
from collections import Counter
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class Put:
    pk: str
    sk: str
    item: dict


@dataclass(frozen=True)
class Replace:
    pk: str
    sk: str
    item: dict
    expected_version: int


@dataclass(frozen=True)
class Increment:
    pk: str
    sk: str
    deltas: dict


@dataclass(frozen=True)
class Check:
    pk: str
    sk: str
    field: str
    value: object


class ConditionFailed(Exception):
    """A condition failed; ``index`` is the first failing operation."""

    def __init__(self, index, reason=""):
        super().__init__(f"condition failed at operation {index}: {reason}")
        self.index = index
        self.reason = reason


class Contention(Exception):
    """Transient conflict (e.g. DynamoDB TransactionConflict or SQLite busy). Safe to retry."""


RESERVED = {"pk", "sk", "_v"}


def merge_increments(ops):
    """Combine Increment operations on the same item into one (one action per item)."""
    merged, order = {}, []
    out = []
    for op in ops:
        if isinstance(op, Increment):
            key = (op.pk, op.sk)
            if key not in merged:
                merged[key] = Counter()
                order.append((len(out), key))
                out.append(None)
            merged[key].update(op.deltas)
        else:
            out.append(op)
    for index, key in order:
        deltas = {k: v for k, v in merged[key].items() if v != 0}
        out[index] = Increment(key[0], key[1], deltas)
    return [op for op in out if not (isinstance(op, Increment) and not op.deltas)]


def _validate_ops(ops):
    seen = set()
    for op in ops:
        key = (op.pk, op.sk)
        if key in seen:
            raise ValueError(f"transaction touches {key} more than once")
        seen.add(key)
        if isinstance(op, (Put, Replace)) and RESERVED & set(op.item):
            raise ValueError("items must not contain pk, sk or _v")
        if isinstance(op, Increment):
            for name, delta in op.deltas.items():
                if type(delta) is not int or name in RESERVED:
                    raise ValueError("increment deltas must be integers on data fields")
    if not ops or len(ops) > 100:
        raise ValueError("a transaction needs 1..100 operations")


class BaseStore:
    backend = "base"

    def __init__(self):
        self.usage = Counter()
        self.before_transact = []  # test hooks: callables(ops) run before executing

    def _run_hooks(self, ops):
        for hook in list(self.before_transact):
            hook(ops)

    def transact(self, ops):
        ops = list(ops)
        _validate_ops(ops)
        self._run_hooks(ops)
        self.usage["transactions"] += 1
        self.usage["transaction_items"] += len(ops)
        self._transact(ops)


class SqliteStore(BaseStore):
    backend = "sqlite"

    def __init__(self, path):
        super().__init__()
        self.path = str(path)
        if self.path != ":memory:":
            Path(self.path).parent.mkdir(parents=True, exist_ok=True)
        self._local = threading.local()
        with self._conn() as conn:
            conn.executescript("""
              CREATE TABLE IF NOT EXISTS items(
                pk TEXT NOT NULL, sk TEXT NOT NULL, v INTEGER NOT NULL, data TEXT NOT NULL,
                PRIMARY KEY(pk, sk));
              CREATE TRIGGER IF NOT EXISTS journal_is_immutable BEFORE UPDATE ON items
                WHEN OLD.sk LIKE '%#JOURNAL#%'
                BEGIN SELECT RAISE(ABORT, 'journal entries are immutable'); END;
            """)

    def _conn(self):
        conn = getattr(self._local, "conn", None)
        if conn is None:
            conn = sqlite3.connect(self.path, timeout=10, isolation_level=None, check_same_thread=False)
            conn.execute("PRAGMA journal_mode=WAL")
            conn.execute("PRAGMA busy_timeout=10000")
            self._local.conn = conn
        return conn

    def close(self):
        conn = getattr(self._local, "conn", None)
        if conn is not None:
            conn.close()
            self._local.conn = None

    @staticmethod
    def _decode(pk, sk, v, data):
        item = json.loads(data)
        item.update(pk=pk, sk=sk, _v=v)
        return item

    def get(self, pk, sk):
        self.usage["reads"] += 1
        row = self._conn().execute("SELECT v, data FROM items WHERE pk=? AND sk=?", (pk, sk)).fetchone()
        return self._decode(pk, sk, *row) if row else None

    def query(self, pk, prefix=""):
        self.usage["queries"] += 1
        rows = self._conn().execute(
            "SELECT sk, v, data FROM items WHERE pk=? AND substr(sk, 1, ?)=? ORDER BY sk",
            (pk, len(prefix), prefix)).fetchall()
        return [self._decode(pk, sk, v, data) for sk, v, data in rows]

    def delete_prefix(self, pk, prefix, keep_prefix=None):
        conn = self._conn()
        sks = [sk for (sk,) in conn.execute(
            "SELECT sk FROM items WHERE pk=? AND substr(sk, 1, ?)=?", (pk, len(prefix), prefix))
            if not (keep_prefix and sk.startswith(keep_prefix))]
        conn.execute("BEGIN IMMEDIATE")
        try:
            conn.executemany("DELETE FROM items WHERE pk=? AND sk=?", [(pk, sk) for sk in sks])
            conn.execute("COMMIT")
        except BaseException:
            conn.execute("ROLLBACK")
            raise
        self.usage["deletes"] += len(sks)
        return len(sks)

    def _transact(self, ops):
        conn = self._conn()
        try:
            conn.execute("BEGIN IMMEDIATE")
        except sqlite3.OperationalError as exc:
            raise Contention(str(exc)) from exc
        try:
            for index, op in enumerate(ops):
                row = conn.execute("SELECT v, data FROM items WHERE pk=? AND sk=?", (op.pk, op.sk)).fetchone()
                if isinstance(op, Put):
                    if row:
                        raise ConditionFailed(index, "item exists")
                    conn.execute("INSERT INTO items VALUES(?,?,?,?)", (op.pk, op.sk, 1, json.dumps(op.item)))
                elif isinstance(op, Replace):
                    if not row or row[0] != op.expected_version:
                        raise ConditionFailed(index, "version mismatch")
                    conn.execute("UPDATE items SET v=?, data=? WHERE pk=? AND sk=?",
                                 (op.expected_version + 1, json.dumps(op.item), op.pk, op.sk))
                elif isinstance(op, Increment):
                    if not row:
                        raise ConditionFailed(index, "item missing")
                    data = json.loads(row[1])
                    for name, delta in op.deltas.items():
                        if data.get(name, 0) + delta < 0:
                            raise ConditionFailed(index, f"{name} would be negative")
                        data[name] = data.get(name, 0) + delta
                    conn.execute("UPDATE items SET v=?, data=? WHERE pk=? AND sk=?",
                                 (row[0] + 1, json.dumps(data), op.pk, op.sk))
                elif isinstance(op, Check):
                    if not row or json.loads(row[1]).get(op.field) != op.value:
                        raise ConditionFailed(index, "check failed")
                else:
                    raise TypeError(op)
            conn.execute("COMMIT")
        except BaseException:
            conn.execute("ROLLBACK")
            raise


class DynamoStore(BaseStore):
    """DynamoDB backend using TransactWriteItems with condition expressions.

    Durable idempotency comes from stored records plus conditional writes, not from
    ClientRequestToken (which DynamoDB only honours for ten minutes), so none is sent.
    """

    backend = "dynamodb"

    def __init__(self, table_name, client=None):
        super().__init__()
        if client is None:
            import boto3
            client = boto3.client("dynamodb")
        from boto3.dynamodb.types import TypeDeserializer, TypeSerializer
        self.table = table_name
        self.client = client
        self._ser = TypeSerializer()
        self._de = TypeDeserializer()
        self.consumed_capacity = Counter()

    def _key(self, pk, sk):
        return {"pk": {"S": pk}, "sk": {"S": sk}}

    def _to_attrs(self, pk, sk, version, item):
        attrs = {k: self._ser.serialize(v) for k, v in item.items()}
        attrs.update(pk={"S": pk}, sk={"S": sk}, _v={"N": str(version)})
        return attrs

    def _plain(self, value):
        from decimal import Decimal
        if isinstance(value, Decimal):
            return int(value) if value == value.to_integral_value() else float(value)
        if isinstance(value, dict):
            return {k: self._plain(v) for k, v in value.items()}
        if isinstance(value, (list, tuple)):
            return [self._plain(v) for v in value]
        if isinstance(value, set):
            return sorted(self._plain(v) for v in value)
        return value

    def _from_attrs(self, attrs):
        return {k: self._plain(self._de.deserialize(v)) for k, v in attrs.items()}

    def _record_capacity(self, response):
        caps = response.get("ConsumedCapacity")
        for cap in caps if isinstance(caps, list) else ([caps] if caps else []):
            for name in ("CapacityUnits", "ReadCapacityUnits", "WriteCapacityUnits"):
                if name in cap:
                    self.consumed_capacity[name] += float(cap[name])

    def get(self, pk, sk):
        self.usage["reads"] += 1
        response = self.client.get_item(TableName=self.table, Key=self._key(pk, sk),
                                        ConsistentRead=True, ReturnConsumedCapacity="TOTAL")
        self._record_capacity(response)
        return self._from_attrs(response["Item"]) if "Item" in response else None

    def _query(self, pk, prefix, projection=False):
        kwargs = {
            "TableName": self.table, "ConsistentRead": True, "ReturnConsumedCapacity": "TOTAL",
            "KeyConditionExpression": "#pk = :pk AND begins_with(#sk, :prefix)",
            "ExpressionAttributeNames": {"#pk": "pk", "#sk": "sk"},
            "ExpressionAttributeValues": {":pk": {"S": pk}, ":prefix": {"S": prefix}},
        }
        if projection:
            kwargs["ProjectionExpression"] = "#pk, #sk"
        items = []
        while True:
            self.usage["queries"] += 1
            response = self.client.query(**kwargs)
            self._record_capacity(response)
            items.extend(self._from_attrs(i) for i in response.get("Items", []))
            if "LastEvaluatedKey" not in response:
                return items
            kwargs["ExclusiveStartKey"] = response["LastEvaluatedKey"]

    def query(self, pk, prefix=""):
        if not prefix:
            raise ValueError("DynamoStore.query requires a sort-key prefix")
        return self._query(pk, prefix)

    def delete_prefix(self, pk, prefix, keep_prefix=None):
        if not prefix:
            raise ValueError("delete_prefix requires a sort-key prefix")
        keys = [i["sk"] for i in self._query(pk, prefix, projection=True)
                if not (keep_prefix and i["sk"].startswith(keep_prefix))]
        for start in range(0, len(keys), 25):
            pending = [{"DeleteRequest": {"Key": self._key(pk, sk)}} for sk in keys[start:start + 25]]
            for _ in range(8):
                response = self.client.batch_write_item(RequestItems={self.table: pending})
                pending = response.get("UnprocessedItems", {}).get(self.table, [])
                if not pending:
                    break
            if pending:
                raise Contention("unprocessed deletes remain; retry reset")
        self.usage["deletes"] += len(keys)
        return len(keys)

    def _action(self, op):
        names = {"#pk": "pk", "#v": "_v"}
        if isinstance(op, Put):
            return {"Put": {"TableName": self.table, "Item": self._to_attrs(op.pk, op.sk, 1, op.item),
                            "ConditionExpression": "attribute_not_exists(#pk)",
                            "ExpressionAttributeNames": {"#pk": "pk"}}}
        if isinstance(op, Replace):
            return {"Put": {"TableName": self.table,
                            "Item": self._to_attrs(op.pk, op.sk, op.expected_version + 1, op.item),
                            "ConditionExpression": "#v = :expected",
                            "ExpressionAttributeNames": {"#v": "_v"},
                            "ExpressionAttributeValues": {":expected": {"N": str(op.expected_version)}}}}
        if isinstance(op, Increment):
            sets, conditions, values = ["#v = #v + :one"], ["attribute_exists(#pk)"], {":one": {"N": "1"}}
            for i, (name, delta) in enumerate(sorted(op.deltas.items())):
                names[f"#f{i}"] = name
                values[f":d{i}"] = {"N": str(delta)}
                sets.append(f"#f{i} = #f{i} + :d{i}")
                if delta < 0:
                    values[f":m{i}"] = {"N": str(-delta)}
                    conditions.append(f"#f{i} >= :m{i}")
            return {"Update": {"TableName": self.table, "Key": self._key(op.pk, op.sk),
                               "UpdateExpression": "SET " + ", ".join(sets),
                               "ConditionExpression": " AND ".join(conditions),
                               "ExpressionAttributeNames": names, "ExpressionAttributeValues": values}}
        if isinstance(op, Check):
            return {"ConditionCheck": {"TableName": self.table, "Key": self._key(op.pk, op.sk),
                                       "ConditionExpression": "#f = :value",
                                       "ExpressionAttributeNames": {"#f": op.field},
                                       "ExpressionAttributeValues": {":value": self._ser.serialize(op.value)}}}
        raise TypeError(op)

    def _transact(self, ops):
        from botocore.exceptions import ClientError
        try:
            response = self.client.transact_write_items(
                TransactItems=[self._action(op) for op in ops], ReturnConsumedCapacity="TOTAL")
            self._record_capacity(response)
        except ClientError as exc:
            error = exc.response.get("Error", {})
            code = error.get("Code", "")
            if code == "TransactionCanceledException":
                reasons = exc.response.get("CancellationReasons") or []
                codes = [r.get("Code", "None") for r in reasons]
                for index, reason in enumerate(codes):
                    if reason == "ConditionalCheckFailed":
                        raise ConditionFailed(index, reason) from exc
                if any(c in ("TransactionConflict", "ThrottlingError", "ProvisionedThroughputExceeded")
                       for c in codes):
                    raise Contention(",".join(codes)) from exc
                message = error.get("Message", "")
                if "ConditionalCheckFailed" in message and not reasons:
                    # Older SDKs only surface reasons in the message, e.g. "[None, ConditionalCheckFailed]".
                    listed = message[message.find("[") + 1:message.rfind("]")].split(",")
                    for index, reason in enumerate(c.strip() for c in listed):
                        if reason == "ConditionalCheckFailed":
                            raise ConditionFailed(index, reason) from exc
                if "TransactionConflict" in message:
                    raise Contention(message) from exc
                raise
            if code in ("TransactionInProgressException", "ThrottlingException",
                        "ProvisionedThroughputExceededException", "RequestLimitExceeded"):
                raise Contention(code) from exc
            raise
