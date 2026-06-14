import json
import re
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
