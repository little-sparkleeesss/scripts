#!/usr/bin/env python3
import os
import re
import sys
from datetime import datetime, timedelta

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from lib.config_utils import load_yaml, resolve_ssh_paths, script_dir
from lib.logging_utils import setup_logger
from lib.ssh_utils import connect_ssh, run_remote_cmd

log = setup_logger("zfs_snapshot")

SCRIPT_DIR = script_dir(__file__)
CONFIG_FILE = os.path.join(SCRIPT_DIR, "config.yaml")

TIMESTAMP_FORMAT = "%Y_%m_%d-%H%M%S"
SNAPSHOT_RE = re.compile(
    r"@([\w-]+)-(\d{4}_\d{2}_\d{2}-\d{6})$"
)
ZFS_NAME_RE = re.compile(r"^[a-zA-Z0-9][a-zA-Z0-9_.\-:/@]*$")


def _parse_snapshot_name(full_name):
    match = SNAPSHOT_RE.search(full_name)
    if not match:
        return None, None
    prefix = match.group(1)
    try:
        ts = datetime.strptime(match.group(2), TIMESTAMP_FORMAT)
    except ValueError:
        return prefix, None
    return prefix, ts


def create_snapshots(ssh, cfg, timestamp):
    prefix = cfg["snapshot"]["prefix"]
    snap_name = f"{prefix}-{timestamp}"

    for entry in cfg["snapshot"]["datasets"]:
        dataset = entry["name"]
        recursive = entry.get("recursive", False)
        full = f"{dataset}@{snap_name}"

        flag = " -r" if recursive else ""
        cmd = f"zfs snapshot{flag} {full}"

        try:
            run_remote_cmd(ssh, cmd)
            log.info(f"快照已创建: {full} (recursive={recursive})")
        except RuntimeError as e:
            log.error(f"快照创建失败: {full} — {e}")


def cleanup_old_snapshots(ssh, cfg, now):
    prefix = cfg["snapshot"]["prefix"]
    retention_days = cfg["snapshot"].get("retention_days", 30)
    cutoff = now.replace(microsecond=0) - timedelta(days=retention_days)

    for entry in cfg["snapshot"]["datasets"]:
        dataset = entry["name"]
        recursive = entry.get("recursive", False)

        flag = " -r" if recursive else ""
        list_cmd = f"zfs list -H -t snapshot -o name{flag} {dataset}"

        try:
            output = run_remote_cmd(ssh, list_cmd)
        except RuntimeError:
            log.warning(f"无法列出 {dataset} 的快照，跳过清理")
            continue

        for line in output.strip().split("\n"):
            line = line.strip()
            if not line:
                continue
            if not ZFS_NAME_RE.match(line):
                log.warning(f"跳过非法 ZFS 名称: {line}")
                continue

            snap_prefix, snap_time = _parse_snapshot_name(line)
            if snap_prefix != prefix or snap_time is None:
                continue

            if snap_time < cutoff:
                try:
                    run_remote_cmd(ssh, f"zfs destroy {line}")
                    log.info(f"已删除过期快照: {line}")
                except RuntimeError as e:
                    log.warning(f"删除快照失败: {line} — {e}")


def main():
    cfg = load_yaml(CONFIG_FILE)
    resolve_ssh_paths(cfg["ssh"], SCRIPT_DIR)

    ssh = connect_ssh(cfg["ssh"])

    try:
        timestamp = datetime.now().strftime(TIMESTAMP_FORMAT)
        now = datetime.now()
        log.info(f"开始 ZFS 快照任务 (timestamp={timestamp})")

        create_snapshots(ssh, cfg, timestamp)
        cleanup_old_snapshots(ssh, cfg, now)

        log.info("ZFS 快照任务完成")
    finally:
        ssh.close()


if __name__ == "__main__":
    main()
