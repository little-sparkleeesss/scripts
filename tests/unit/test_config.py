import os
import tempfile
from pathlib import Path
from unittest import mock

import pytest

from lib.config_utils import (
    ConfigError,
    load_yaml,
    resolve_path,
    resolve_pg_paths,
    resolve_ssh_paths,
    script_dir,
)


class TestLoadYaml:
    def test_loads_valid_yaml(self):
        with tempfile.NamedTemporaryFile("w", suffix=".yaml", delete=False) as f:
            f.write("key: value\n")
        try:
            cfg = load_yaml(f.name)
            assert cfg == {"key": "value"}
        finally:
            os.unlink(f.name)

    def test_empty_file_raises(self):
        with tempfile.NamedTemporaryFile("w", suffix=".yaml", delete=False) as f:
            f.write("")
        try:
            with pytest.raises(ConfigError):
                load_yaml(f.name)
        finally:
            os.unlink(f.name)

    def test_missing_file_raises(self):
        with pytest.raises(ConfigError):
            load_yaml("/nonexistent/config.yaml")

    def test_invalid_yaml_raises(self):
        with tempfile.NamedTemporaryFile("w", suffix=".yaml", delete=False) as f:
            f.write("{bad")
        try:
            with pytest.raises(ConfigError):
                load_yaml(f.name)
        finally:
            os.unlink(f.name)

    def test_top_level_list_raises(self):
        with tempfile.NamedTemporaryFile("w", suffix=".yaml", delete=False) as f:
            f.write("- item\n")
        try:
            with pytest.raises(ConfigError):
                load_yaml(f.name)
        finally:
            os.unlink(f.name)


class TestResolvePath:
    def test_absolute_passes_through(self):
        assert resolve_path("/tmp", "/etc/hosts") == "/etc/hosts"

    def test_relative_joins(self):
        result = resolve_path("/tmp", "sub/file.txt")
        assert result == os.path.normpath("/tmp/sub/file.txt")

    def test_empty_returns_empty(self):
        assert resolve_path("/tmp", "") == ""


class TestScriptDir:
    def test_returns_absolute(self):
        d = script_dir(__file__)
        assert os.path.isabs(d)
        assert d.endswith("unit")


class TestResolveSshPaths:
    def test_resolves_private_key(self):
        cfg = {"private_key": "keys/id_ed25519"}
        result = resolve_ssh_paths(cfg, "/app")
        assert result["private_key"] == os.path.normpath("/app/keys/id_ed25519")

    def test_resolves_aliased_key_field(self):
        cfg = {"key": "keys/secret"}
        result = resolve_ssh_paths(cfg, "/app")
        assert result["key"] == os.path.normpath("/app/keys/secret")

    def test_ignores_absolute_key(self):
        cfg = {"private_key": "/etc/id_ed25519"}
        result = resolve_ssh_paths(cfg, "/app")
        assert result["private_key"] == "/etc/id_ed25519"

    def test_ignores_none_key(self):
        cfg = {"host": "x", "private_key": None}
        result = resolve_ssh_paths(cfg, "/app")
        assert result["private_key"] is None


class TestResolvePgPaths:
    def test_resolves_ssl_cert(self):
        cfg = {"ssl_root_cert": "certs/ca.crt"}
        result = resolve_pg_paths(cfg, "/app")
        assert result["ssl_root_cert"] == os.path.normpath("/app/certs/ca.crt")

    def test_resolves_mtls_certs(self):
        cfg = {"mtls": {"client_cert": "certs/cert.crt", "client_key": "certs/key.key"}}
        result = resolve_pg_paths(cfg, "/app")
        assert result["mtls"]["client_cert"] == os.path.normpath("/app/certs/cert.crt")
        assert result["mtls"]["client_key"] == os.path.normpath("/app/certs/key.key")

    def test_ignores_empty_mtls(self):
        cfg = {"mtls": {}}
        result = resolve_pg_paths(cfg, "/app")
        assert result["mtls"] == {}
