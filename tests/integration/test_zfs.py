import os
import re

import pytest

from zfs_snapshot.zfs_snapshot import (
    MAX_DELETIONS,
    SNAPSHOT_RE,
    ZFS_NAME_RE,
    _check_overlapping_datasets,
    _parse_snapshot_name,
)

ZFS_HOST = os.environ.get("ZFS_REMOTE_HOST", "")


class TestParseSnapshotName:
    def test_valid_snapshot(self):
        prefix, ts = _parse_snapshot_name("tank/data@auto-2026_01_15-120000")
        assert prefix == "auto"
        assert ts is not None

    def test_wrong_prefix_separator(self):
        prefix, ts = _parse_snapshot_name("tank/data@other-2026_01_15-120000")
        assert prefix == "other"

    def test_invalid_date(self):
        prefix, ts = _parse_snapshot_name("tank/data@auto-9999_99_99-999999")
        assert prefix == "auto"
        assert ts is None

    def test_no_at_sign(self):
        prefix, ts = _parse_snapshot_name("tank/data")
        assert prefix is None
        assert ts is None


class TestZfsNameRe:
    def test_valid_dataset(self):
        assert ZFS_NAME_RE.match("tank/data/sub") is not None

    def test_rejects_injection(self):
        assert ZFS_NAME_RE.match("tank; rm -rf /") is None
        assert ZFS_NAME_RE.match("tank`whoami`") is None
        assert ZFS_NAME_RE.match('tank"') is None


class TestCheckOverlapping:
    def test_warns_on_sub_dataset(self, caplog):
        datasets = [
            {"name": "tank/data", "recursive": True},
            {"name": "tank/data/sub", "recursive": False},
        ]
        _check_overlapping_datasets(datasets)
        assert "tank/data/sub" in caplog.text

    def test_no_warn_on_siblings(self, caplog):
        datasets = [
            {"name": "tank/apps", "recursive": False},
            {"name": "tank/media", "recursive": False},
        ]
        _check_overlapping_datasets(datasets)
        assert caplog.text == ""


class TestMaxDeletions:
    def test_max_deletions_is_positive(self):
        assert MAX_DELETIONS > 0


# ── ZFS_REMOTE_HOST 不为空时启用远端测试 ──────────────────────────

@pytest.mark.skipif(not ZFS_HOST, reason="ZFS_REMOTE_HOST 未设置，跳过远端 ZFS 测试")
class TestZfsRemote:
    @pytest.fixture(scope="class")
    def zfs_ssh(self):
        from lib.ssh_utils import connect_ssh
        import shlex

        user_host = ZFS_HOST
        port = 22
        if ":" in ZFS_HOST:
            parts = ZFS_HOST.rsplit(":", 1)
            user_host = parts[0]
            port = int(parts[1])

        user, host = user_host.split("@", 1)
        key_path = os.path.expanduser(
            os.environ.get("ZFS_SSH_KEY", "~/.ssh/id_ed25519")
        )

        client = connect_ssh({
            "host": host,
            "port": port,
            "user": user,
            "key": key_path,
            "known_host_key": os.environ.get("ZFS_KNOWN_HOST_KEY", ""),
        })
        yield client
        try:
            client.close()
        except Exception:
            pass

    def test_zfs_list_works(self, zfs_ssh):
        from lib.ssh_utils import run_remote_cmd
        out = run_remote_cmd(zfs_ssh, "zfs list -H -o name")
        assert len(out.strip()) > 0

    def test_zfs_snapshot_command_format(self, zfs_ssh):
        from lib.ssh_utils import run_remote_cmd
        dataset = os.environ.get("ZFS_TEST_DATASET", "")
        if not dataset:
            pytest.skip("ZFS_TEST_DATASET 未设置")
        snap_name = f"pytest_integration-{int(__import__('time').time())}"
        try:
            run_remote_cmd(zfs_ssh, f"zfs snapshot {dataset}@{snap_name}")
            out = run_remote_cmd(zfs_ssh, f"zfs list -H -t snapshot -o name {dataset}")
            assert snap_name in out
        finally:
            try:
                run_remote_cmd(zfs_ssh, f"zfs destroy {dataset}@{snap_name}")
            except Exception:
                pass
