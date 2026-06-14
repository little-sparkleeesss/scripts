import json
from datetime import datetime, timezone

import pytest

from smart_monitor.smart_push import (
    _add_missing_columns,
    _ensure_schema,
    _insert_row,
)


@pytest.fixture(autouse=True)
def _cleanup_table(pg_conn):
    cur = pg_conn.cursor()
    cur.execute("DROP TABLE IF EXISTS smart_test CASCADE")
    cur.close()
    yield
    cur = pg_conn.cursor()
    cur.execute("DROP TABLE IF EXISTS smart_test CASCADE")
    cur.close()


DATA1 = {
    "timestamp": datetime(2026, 1, 1, tzinfo=timezone.utc),
    "serial": "DISK001",
    "model": "Some SSD",
    "Raw_Read_Error_Rate": json.dumps({"value": 100, "raw": "0"}),
}

DATA2 = {
    "timestamp": datetime(2026, 1, 2, tzinfo=timezone.utc),
    "serial": "DISK002",
    "model": "Other HDD",
    "Raw_Read_Error_Rate": json.dumps({"value": 95, "raw": "0"}),
    "Spin_Up_Time": json.dumps({"value": 200, "raw": "0"}),
}


class TestSchemaAndInsert:
    def test_creates_table_and_inserts(self, pg_conn):
        safe, existing = _ensure_schema(pg_conn, "smart_test", DATA1)
        assert safe == "smart_test"
        assert "id" in existing
        assert "serial" in existing

        cur = pg_conn.cursor()
        cur.execute("SELECT count(*) FROM smart_test")
        assert cur.fetchone() == (0,)
        cur.close()

        _insert_row(pg_conn, safe, DATA1)

        cur = pg_conn.cursor()
        cur.execute("SELECT serial, model FROM smart_test")
        rows = cur.fetchall()
        assert len(rows) == 1
        assert rows[0] == ("DISK001", "Some SSD")
        cur.close()

    def test_adds_missing_columns(self, pg_conn):
        safe, existing = _ensure_schema(pg_conn, "smart_test", DATA1)
        _insert_row(pg_conn, safe, DATA1)

        _add_missing_columns(pg_conn, safe, existing, DATA2)
        assert "Spin_Up_Time" in existing

        _insert_row(pg_conn, safe, DATA2)

        cur = pg_conn.cursor()
        cur.execute("SELECT serial, model, Spin_Up_Time FROM smart_test ORDER BY id")
        rows = cur.fetchall()
        assert len(rows) == 2
        assert json.loads(rows[1][2])["value"] == 200
        cur.close()

    def test_ensure_schema_idempotent(self, pg_conn):
        safe1, existing1 = _ensure_schema(pg_conn, "smart_test", DATA1)
        safe2, existing2 = _ensure_schema(pg_conn, "smart_test", DATA1)
        assert safe1 == safe2
        assert existing1 == existing2
