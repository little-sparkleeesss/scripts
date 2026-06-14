#!/usr/bin/env python3
import os
import re
import shlex
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

MAX_DELETIONS = 100


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
        cmd = (
            f"zfs snapshot{flag}"
            f" {shlex.quote(full)}"
        )

        try:
            run_remote_cmd(ssh, cmd)
            log.info(f"快照已创建: {full} (recursive={recursive})")
        except RuntimeError as e:
            msg = str(e)
            if "dataset already exists" in msg.lower():
                log.info(f"快照已存在（可能由上层递归创建）: {full}")
            else:
                log.error(f"快照创建失败: {full} — {e}")


def cleanup_old_snapshots(ssh, cfg, now):
    prefix = cfg["snapshot"]["prefix"]
    retention_days = cfg["snapshot"].get("retention_days", 30)
    cutoff = now.replace(microsecond=0) - timedelta(days=retention_days)

    candidates = []

    for entry in cfg["snapshot"]["datasets"]:
        dataset = entry["name"]
        recursive = entry.get("recursive", False)

        flag = " -r" if recursive else ""
        escaped_dataset = shlex.quote(dataset)
        list_cmd = (
            f"zfs list -H -t snapshot -o name{flag}"
            f" {escaped_dataset}"
        )

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
                candidates.append(line)

    if not candidates:
        return

    candidates = sorted(set(candidates))

    if len(candidates) > MAX_DELETIONS:
        log.error(
            f"待删除快照数量 ({len(candidates)}) 超过安全上限 ({MAX_DELETIONS})，"
            f"跳过清理以防止误删。请检查 retention_days 或手动处理。"
        )
        return

    log.info(f"共 {len(candidates)} 个过期快照待删除")

    for snap in candidates:
        try:
            run_remote_cmd(ssh, f"zfs destroy {shlex.quote(snap)}")
            log.info(f"已删除过期快照: {snap}")
        except RuntimeError as e:
            log.warning(f"删除快照失败: {snap} — {e}")


def _check_overlapping_datasets(datasets):
    for i, a in enumerate(datasets):
        for b in datasets[i + 1:]:
            a_name, b_name = a["name"], b["name"]
            if a.get("recursive") and (b_name == a_name or b_name.startswith(a_name + "/")):
                log.warning(
                    f"数据集 {b_name!r} 已被 {a_name!r} (recursive=true) 覆盖，"
                    f"建议从配置中移除以避免重复操作"
                )
            if b.get("recursive") and (a_name == b_name or a_name.startswith(b_name + "/")):
                log.warning(
                    f"数据集 {a_name!r} 已被 {b_name!r} (recursive=true) 覆盖，"
                    f"建议从配置中移除以避免重复操作"
                )


def main():
    cfg = load_yaml(CONFIG_FILE)
    resolve_ssh_paths(cfg["ssh"], SCRIPT_DIR)

    prefix = cfg["snapshot"]["prefix"]
    if not prefix or not prefix.strip():
        log.error("snapshot.prefix 不能为空，拒绝运行以避免误删其他快照")
        sys.exit(1)
    if not ZFS_NAME_RE.match(prefix):
        log.error(f"snapshot.prefix 包含非法字符: {prefix!r}")
        sys.exit(1)

    _check_overlapping_datasets(cfg["snapshot"]["datasets"])

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
