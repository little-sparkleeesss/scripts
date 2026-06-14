import json
from datetime import datetime, timezone

import pytest

from smart_monitor.smart_push import clean_column_name, parse_smart_json


class TestCleanColumnName:
    def test_replaces_punctuation_with_underscore(self):
        assert clean_column_name("Raw-Read_Error.Rate") == "Raw_Read_Error_Rate"

    def test_prefixes_leading_digit(self):
        assert clean_column_name("1_Power") == "_1_Power"

    def test_empty_returns_empty(self):
        assert clean_column_name("") == ""

    def test_all_alnum_passes_through(self):
        assert clean_column_name("Reallocated_Sector_Ct") == "Reallocated_Sector_Ct"


class TestParseSmartJson:
    SAMPLE_JSON = json.dumps({
        "model_name": "Samsung SSD 860 EVO 1TB",
        "serial_number": "S3Z8NB0M123456",
        "local_time": {"time_t": 1609459200},
        "ata_smart_attributes": {
            "table": [
                {"name": "Raw_Read_Error_Rate", "value": 100, "worst": 100, "thresh": 50, "raw": {"string": "0"}},
                {"name": "Reallocated_Sector_Ct", "value": 100, "worst": 100, "thresh": 10, "raw": {"string": "0"}},
            ]
        },
    })

    def test_parses_serial_and_model(self):
        result = parse_smart_json(self.SAMPLE_JSON)
        assert result["serial"] == "S3Z8NB0M123456"
        assert result["model"] == "Samsung SSD 860 EVO 1TB"

    def test_parses_timestamp(self):
        result = parse_smart_json(self.SAMPLE_JSON)
        assert result["timestamp"] == datetime(2021, 1, 1, tzinfo=timezone.utc)

    def test_parses_attributes(self):
        result = parse_smart_json(self.SAMPLE_JSON)
        assert "Raw_Read_Error_Rate" in result
        attr = json.loads(result["Raw_Read_Error_Rate"])
        assert attr["value"] == 100
        assert attr["raw"] == "0"

    def test_returns_none_for_invalid_json(self):
        assert parse_smart_json("not json") is None

    def test_no_timestamp_returns_none_timestamp(self):
        j = json.dumps({"model_name": "X", "ata_smart_attributes": {"table": []}})
        result = parse_smart_json(j)
        assert result["timestamp"] is None

    def test_empty_attributes_is_fine(self):
        j = json.dumps({"model_name": "X", "ata_smart_attributes": {"table": []}})
        result = parse_smart_json(j)
        assert result["model"] == "X"


NVME_SAMPLE_JSON = json.dumps({
    "device": {"name": "/dev/nvme0n1", "type": "nvme", "protocol": "NVMe"},
    "model_name": "WDS250G3X0C-00SJG0",
    "serial_number": "REDACTED",
    "local_time": {"time_t": 1781436805},
    "smart_status": {"passed": True},
    "nvme_smart_health_information_log": {
        "nsid": -1,
        "critical_warning": 0,
        "temperature": 52,
        "available_spare": 100,
        "available_spare_threshold": 10,
        "percentage_used": 0,
        "data_units_read": 75416025,
        "data_units_written": 81263944,
        "host_reads": 961531687,
        "host_writes": 1040466201,
        "controller_busy_time": 2687,
        "power_cycles": 10928,
        "power_on_hours": 12879,
        "unsafe_shutdowns": 1383,
        "media_errors": 0,
        "num_err_log_entries": 0,
        "warning_temp_time": 0,
        "critical_comp_time": 0,
    },
})


class TestParseNvmeSmartJson:
    def test_parses_device_type_and_status(self):
        result = parse_smart_json(NVME_SAMPLE_JSON)
        assert result["device_type"] == "nvme"
        assert result["smart_status_passed"] is True

    def test_parses_serial_and_model(self):
        result = parse_smart_json(NVME_SAMPLE_JSON)
        assert result["serial"] == "REDACTED"
        assert result["model"] == "WDS250G3X0C-00SJG0"

    def test_parses_nvme_health_fields_as_ints(self):
        result = parse_smart_json(NVME_SAMPLE_JSON)
        assert result["temperature"] == 52
        assert result["available_spare"] == 100
        assert result["power_on_hours"] == 12879
        assert result["data_units_read"] == 75416025
        assert isinstance(result["temperature"], int)

    def test_skips_nsid(self):
        result = parse_smart_json(NVME_SAMPLE_JSON)
        assert "nsid" not in result

    def test_no_ata_attributes_in_nvme(self):
        result = parse_smart_json(NVME_SAMPLE_JSON)
        # NVMe 盘不应有 ATA 属性名为 key 的项
        ata_like = [k for k in result if "_" not in k and not k.islower()]
        assert not ata_like

    def test_stores_smart_status_as_bool(self):
        result = parse_smart_json(NVME_SAMPLE_JSON)
        assert isinstance(result["smart_status_passed"], bool)

    def test_total_field_count(self):
        result = parse_smart_json(NVME_SAMPLE_JSON)
        # ts + model + serial + device_type + smart_status_passed + 16 nvme fields = 21
        assert len(result) == 21
