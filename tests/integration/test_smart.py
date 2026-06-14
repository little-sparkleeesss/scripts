import json
from datetime import datetime, timezone

import pytest

from lib.ssh_utils import run_remote_cmd
from smart_monitor.smart_push import (
    _add_missing_columns,
    _ensure_schema,
    _insert_row,
    parse_smart_json,
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
        cur.execute('SELECT serial, model, "Spin_Up_Time" FROM smart_test ORDER BY id')
        rows = cur.fetchall()
        assert len(rows) == 2
        assert rows[1][2]["value"] == 200
        cur.close()

    def test_ensure_schema_idempotent(self, pg_conn):
        safe1, existing1 = _ensure_schema(pg_conn, "smart_test", DATA1)
        safe2, existing2 = _ensure_schema(pg_conn, "smart_test", DATA1)
        assert safe1 == safe2
        assert existing1 == existing2


SATA_SMARTCTL_JSON = json.dumps({
    "local_time": {"time_t": 1609459200},
    "model_name": "WD Red Plus 4TB",
    "serial_number": "WD-ABC12345",
    "ata_smart_attributes": {
        "table": [
            {"name": "Raw_Read_Error_Rate", "value": 100, "worst": 100, "thresh": 50, "raw": {"string": "0"}},
            {"name": "Reallocated_Sector_Ct", "value": 100, "worst": 100, "thresh": 10, "raw": {"string": "0"}},
            {"name": "Power_On_Hours", "value": 98, "worst": 98, "thresh": 0, "raw": {"string": "12345"}},
        ]
    },
})


class TestSataFullPipeline:
    def test_ssh_echo_parse_insert_query(self, ssh_client, pg_conn):
        cmd = f"echo '{SATA_SMARTCTL_JSON}'"
        output = run_remote_cmd(ssh_client, cmd)
        assert "WD-ABC12345" in output

        data = parse_smart_json(output)
        assert data is not None
        assert data["serial"] == "WD-ABC12345"
        assert data["model"] == "WD Red Plus 4TB"
        assert data["timestamp"] == datetime(2021, 1, 1, tzinfo=timezone.utc)
        assert len(data) == 7  # ts + serial + model + device_type + 3 attrs

        safe, existing = _ensure_schema(pg_conn, "smart_test", data)
        _insert_row(pg_conn, safe, data)

        cur = pg_conn.cursor()
        cur.execute('SELECT serial, model, "Power_On_Hours" FROM smart_test')
        row = cur.fetchone()
        assert row[0] == "WD-ABC12345"
        assert row[1] == "WD Red Plus 4TB"
        p = row[2]
        assert p["raw"] == "12345"
        cur.close()


# ── NVMe 全链路 ────────────────────────────────────────────────────

NVME_SMARTCTL_JSON = json.dumps({
    "local_time": {"time_t": 1609459200},
    "device": {"type": "nvme"},
    "model_name": "Samsung SSD 980 PRO 1TB",
    "serial_number": "NVME-REDACTED",
    "smart_status": {"passed": True},
    "nvme_smart_health_information_log": {
        "nsid": -1,
        "critical_warning": 0,
        "temperature": 42,
        "available_spare": 100,
        "percentage_used": 1,
        "data_units_read": 12345678,
        "data_units_written": 87654321,
        "power_on_hours": 5000,
        "power_cycles": 200,
        "unsafe_shutdowns": 3,
        "media_errors": 0,
    },
})


class TestNvmeFullPipeline:
    def test_ssh_echo_nvme_parse_insert_query(self, ssh_client, pg_conn):
        cmd = f"echo '{NVME_SMARTCTL_JSON}'"
        output = run_remote_cmd(ssh_client, cmd)
        assert "NVME-REDACTED" in output

        data = parse_smart_json(output)
        assert data is not None
        assert data["device_type"] == "nvme"
        assert data["serial"] == "NVME-REDACTED"
        assert data["temperature"] == 42
        assert isinstance(data["smart_status_passed"], bool)

        safe, existing = _ensure_schema(pg_conn, "smart_test", data)
        _insert_row(pg_conn, safe, data)

        cur = pg_conn.cursor()
        cur.execute("SELECT serial, device_type, temperature, power_on_hours, smart_status_passed FROM smart_test")
        row = cur.fetchone()
        assert row[0] == "NVME-REDACTED"
        assert row[1] == "nvme"
        assert row[2] == 42
        assert row[3] == 5000
        assert row[4] is True
        cur.close()

    def test_nvme_columns_have_correct_types(self, pg_conn):
        data = parse_smart_json(NVME_SMARTCTL_JSON)
        _ensure_schema(pg_conn, "smart_test", data)
        _insert_row(pg_conn, "smart_test", data)

        cur = pg_conn.cursor()
        cur.execute("""
            SELECT data_type FROM information_schema.columns
            WHERE table_name = 'smart_test' AND table_schema = 'public'
            AND column_name = 'temperature'
        """)
        assert cur.fetchone()[0] == "bigint"

        cur.execute("""
            SELECT data_type FROM information_schema.columns
            WHERE table_name = 'smart_test' AND table_schema = 'public'
            AND column_name = 'smart_status_passed'
        """)
        assert cur.fetchone()[0] == "boolean"

        cur.execute("""
            SELECT data_type FROM information_schema.columns
            WHERE table_name = 'smart_test' AND table_schema = 'public'
            AND column_name = 'device_type'
        """)
        assert cur.fetchone()[0] in ("text", "character varying")
        cur.close()
