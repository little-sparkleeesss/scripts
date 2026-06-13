#!/usr/bin/env python3
import os
import secrets
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from lib.config_utils import load_yaml, resolve_pg_paths, resolve_ssh_paths, script_dir
from lib.logging_utils import setup_logger
from lib.pg_utils import build_pg_env_string
from lib.ssh_utils import connect_ssh, open_sftp, run_remote_cmd

log = setup_logger("pg_full_database_sync")

SCRIPT_DIR = script_dir(__file__)
CONFIG_FILE = os.path.join(SCRIPT_DIR, "config.yaml")


def sync_database(cfg):
    ssh_src_cfg = resolve_ssh_paths(cfg["source_db"]["ssh"], SCRIPT_DIR)
    src_db = cfg["source_db"]["db"]

    target_db = cfg["local_target_db"]
    ssh_target_cfg = cfg["local_target_db"].get("ssh")
    if not ssh_target_cfg:
        raise RuntimeError("local_target_db 缺少 ssh 节点")
    resolve_ssh_paths(ssh_target_cfg, SCRIPT_DIR)

    options = cfg.get("options", {})
    include_schema = options.get("include_schema", True)
    include_data = options.get("include_data", True)
    clean = options.get("clean", True)

    ssh_src = None
    sftp_src = None
    ssh_tgt = None
    sftp_tgt = None
    local_tmp = None
    remote_tmp_src = None
    remote_tmp_tgt = None

    try:
        # --- 1. 源端 pg_dump ---
        ssh_src = connect_ssh(ssh_src_cfg)
        sftp_src = open_sftp(ssh_src)
        remote_tmp_src = f"/tmp/pgdump_{os.getpid()}_{secrets.token_hex(8)}.sql"

        dump_cmd = "pg_dump --no-password"
        if clean:
            dump_cmd += " --clean"
        if include_schema and not include_data:
            dump_cmd += " --schema-only"
        elif include_data and not include_schema:
            dump_cmd += " --data-only"

        full_cmd = f"{build_pg_env_string(src_db)} {dump_cmd} -f {remote_tmp_src}"
        log.info("在源端执行 pg_dump ...")
        run_remote_cmd(ssh_src, full_cmd)

        # --- 2. 下载 SQL 文件 ---
        local_tmp = os.path.join(
            tempfile.gettempdir(), f"pgdump_{os.getpid()}_{secrets.token_hex(8)}.sql"
        )
        log.info(f"下载: {remote_tmp_src} -> {local_tmp}")
        sftp_src.get(remote_tmp_src, local_tmp)
        sftp_src.remove(remote_tmp_src)
        remote_tmp_src = None

        # --- 3. 上传到目标服务器 ---
        ssh_tgt = connect_ssh(ssh_target_cfg)
        sftp_tgt = open_sftp(ssh_tgt)
        remote_tmp_tgt = f"/tmp/restore_{os.getpid()}_{secrets.token_hex(8)}.sql"
        log.info(f"上传: {local_tmp} -> {remote_tmp_tgt}")
        sftp_tgt.put(local_tmp, remote_tmp_tgt)

        # --- 4. 目标端 psql 恢复 ---
        restore_cmd = (
            f"{build_pg_env_string(target_db)} "
            f"psql --no-password -f {remote_tmp_tgt}"
        )
        log.info("在目标端执行 psql 恢复 ...")
        run_remote_cmd(ssh_tgt, restore_cmd)

        # --- 5. 清理远端临时文件 ---
        sftp_tgt.remove(remote_tmp_tgt)
        remote_tmp_tgt = None

        log.info("同步完成")
    finally:
        for path in (remote_tmp_src, remote_tmp_tgt):
            if not path:
                continue
            sftp = sftp_src if path == remote_tmp_src else sftp_tgt
            if sftp:
                try:
                    sftp.remove(path)
                except Exception:
                    pass
        for c in (sftp_src, sftp_tgt):
            if c:
                c.close()
        for c in (ssh_src, ssh_tgt):
            if c:
                c.close()
        if local_tmp and os.path.exists(local_tmp):
            os.remove(local_tmp)


def main():
    cfg = load_yaml(CONFIG_FILE)
    try:
        sync_database(cfg)
    except Exception as e:
        log.error(f"同步失败: {e}")
        sys.exit(1)


if __name__ == "__main__":
    main()
