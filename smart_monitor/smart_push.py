import json
import os
import sys
from datetime import datetime, timezone

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from lib.config_utils import load_yaml, resolve_pg_paths, resolve_ssh_paths, script_dir
from lib.logging_utils import setup_logger
from lib.pg_utils import connect_postgres
from lib.ssh_utils import connect_ssh, run_remote_cmd

log = setup_logger("smart_push")

SCRIPT_DIR = script_dir(__file__)
CONFIG_FILE = os.path.join(SCRIPT_DIR, "config.yaml")


def run_smartctl(ssh, cmd):
    if "-j" not in cmd:
        cmd += " -j"
    log.info(f"执行命令: {cmd}")
    try:
        return run_remote_cmd(ssh, cmd, timeout=30)
    except RuntimeError as e:
        log.error(f"SMART 数据获取失败: {e}")
        return None


def clean_column_name(name):
    if not name:
        return ""
    safe = "".join(c if c.isalnum() or c == "_" else "_" for c in name)
    if safe[0].isdigit():
        safe = "_" + safe
    return safe


NVME_SKIP_KEYS = {"nsid"}

TEXT_COLS = {"serial", "model", "device_type"}


def _column_type(col, val):
    if col == "timestamp":
        return "TIMESTAMPTZ"
    if col in TEXT_COLS:
        return "TEXT"
    if isinstance(val, bool):
        return "BOOLEAN"
    if isinstance(val, int):
        return "BIGINT"
    if isinstance(val, float):
        return "DOUBLE PRECISION"
    return "JSONB"


def _parse_nvme_health(data, result):
    health = data.get("nvme_smart_health_information_log", {})
    if not health:
        return
    for key, val in health.items():
        if key in NVME_SKIP_KEYS:
            continue
        if isinstance(val, bool):
            result[key] = val
        elif isinstance(val, int):
            result[key] = val
        elif isinstance(val, float):
            result[key] = val
        else:
            result[key] = json.dumps(val)


def parse_smart_json(json_text):
    try:
        data = json.loads(json_text)
    except json.JSONDecodeError as e:
        preview = json_text[:200] if json_text else "(empty)"
        log.error(f"JSON 解析失败: {e} — 原始数据前200字符: {preview}")
        return None

    time_t = (data.get("local_time") or {}).get("time_t")
    ts = datetime.fromtimestamp(time_t, tz=timezone.utc) if time_t else None

    device_type = (data.get("device") or {}).get("type", "")

    result = {
        "timestamp": ts,
        "model": data.get("model_name"),
        "serial": data.get("serial_number"),
        "device_type": device_type or "unknown",
    }

    if data.get("smart_status", {}).get("passed") is not None:
        result["smart_status_passed"] = data["smart_status"]["passed"]

    attributes = data.get("ata_smart_attributes", {}).get("table", [])
    for attr in attributes:
        name = attr.get("name")
        if not name:
            continue
        clean_name = clean_column_name(name)
        result[clean_name] = json.dumps({
            "value": attr.get("value"),
            "worst": attr.get("worst"),
            "threshold": attr.get("thresh"),
            "raw": attr.get("raw", {}).get("string"),
        })

    if device_type == "nvme" or (not attributes and not device_type):
        _parse_nvme_health(data, result)

    return result


def _ensure_schema(conn, table, sample_data):
    safe_table = table.replace('"', '""')
    cur = conn.cursor()
    cur.execute("""
        SELECT column_name FROM information_schema.columns
        WHERE table_name = %s AND table_schema = 'public'
    """, (table,))
    existing = {row[0] for row in cur.fetchall()}

    if not existing:
        log.info("创建新表结构...")
        create_fields = []
        for col in sample_data:
            ct = _column_type(col, sample_data[col])
            create_fields.append(f'"{col}" {ct}')
        cur.execute(f'''
            CREATE TABLE IF NOT EXISTS "{safe_table}" (
                id SERIAL PRIMARY KEY,
                {", ".join(create_fields)}
            );
        ''')
        existing = {"id"} | set(sample_data.keys())
    elif "id" not in existing:
        log.info("添加主键列 id...")
        cur.execute(f'ALTER TABLE "{safe_table}" ADD COLUMN IF NOT EXISTS id SERIAL PRIMARY KEY')
        existing.add("id")

    cur.close()
    conn.commit()
    return safe_table, existing


def _add_missing_columns(conn, safe_table, existing, data):
    missing = [col for col in data if col not in existing]
    if not missing:
        return
    cur = conn.cursor()
    for col in missing:
        ct = _column_type(col, data[col])
        log.info(f"添加缺失字段: {col} ({ct})")
        cur.execute(f'ALTER TABLE "{safe_table}" ADD COLUMN IF NOT EXISTS "{col}" {ct}')
        existing.add(col)
    cur.close()
    conn.commit()


def _insert_row(conn, safe_table, data):
    cur = conn.cursor()
    columns = list(data.keys())
    col_string = ", ".join(f'"{col}"' for col in columns)
    placeholders = ", ".join(["%s"] * len(columns))
    values = [data[col] for col in columns]
    cur.execute(f'''
        INSERT INTO "{safe_table}" ({col_string}) VALUES ({placeholders})
    ''', values)
    cur.close()
    conn.commit()
    log.info(f"已写入数据库: serial={data.get('serial')}")


def main():
    cfg = load_yaml(CONFIG_FILE)
    resolve_ssh_paths(cfg["ssh"], SCRIPT_DIR)
    resolve_pg_paths(cfg["postgres"], SCRIPT_DIR)

    pg_conn = connect_postgres(cfg["postgres"])
    try:
        ssh = connect_ssh(cfg["ssh"])
        try:
            log.info("开始新一轮 SMART 信息采集...")

            pg_table = cfg["postgres"]["table"]
            safe_table = None
            existing = set()

            for dev in cfg["devices"]:
                output = run_smartctl(ssh, dev["cmd"])
                if output is None:
                    log.warning(f"SMART 命令失败，尝试重连 SSH: {dev['cmd']}")
                    try:
                        ssh.close()
                    except Exception as e:
                        log.debug(f"关闭旧 SSH 连接时出现非关键错误: {e}")
                    ssh = connect_ssh(cfg["ssh"])
                    output = run_smartctl(ssh, dev["cmd"])

                if output is None:
                    log.warning(f"重试后仍失败，跳过: {dev['cmd']}")
                    continue

                data = parse_smart_json(output)
                if data:
                    log.info(
                        f"解析结果: serial={data.get('serial')}，共获取 {len(data)} 项"
                    )
                    if safe_table is None:
                        safe_table, existing = _ensure_schema(pg_conn, pg_table, data)
                    else:
                        _add_missing_columns(pg_conn, safe_table, existing, data)
                    _insert_row(pg_conn, safe_table, data)
                else:
                    log.warning(f"SMART JSON 解析失败: {dev['cmd']}")
        finally:
            ssh.close()
    finally:
        pg_conn.close()


if __name__ == "__main__":
    main()
