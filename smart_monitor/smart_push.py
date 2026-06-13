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


def parse_smart_json(json_text):
    try:
        data = json.loads(json_text)
    except json.JSONDecodeError as e:
        preview = json_text[:200] if json_text else "(empty)"
        log.error(f"JSON 解析失败: {e} — 原始数据前200字符: {preview}")
        return None

    time_t = (data.get("local_time") or {}).get("time_t")
    ts = datetime.fromtimestamp(time_t, tz=timezone.utc) if time_t else None

    result = {
        "timestamp": ts,
        "model": data.get("model_name"),
        "serial": data.get("serial_number"),
    }

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

    return result


def push_to_postgres(cfg, data):
    conn = None
    cursor = None
    try:
        conn = connect_postgres(cfg["postgres"])
        cursor = conn.cursor()
        table = cfg["postgres"]["table"]
        safe_table = table.replace('"', '""')

        cursor.execute("""
            SELECT column_name, data_type FROM information_schema.columns
            WHERE table_name = %s AND table_schema = 'public'
        """, (table,))
        existing_columns = {row[0]: row[1] for row in cursor.fetchall()}

        if not existing_columns:
            log.info("创建新表结构...")
            create_fields = []
            for col in data.keys():
                if col == "timestamp":
                    col_type = "TIMESTAMPTZ"
                elif col in ("serial", "model"):
                    col_type = "TEXT"
                else:
                    col_type = "JSONB"
                create_fields.append(f'"{col}" {col_type}')
            cursor.execute(f'''
                CREATE TABLE IF NOT EXISTS "{safe_table}" (
                    id SERIAL PRIMARY KEY,
                    {", ".join(create_fields)}
                );
            ''')
        else:
            if "id" not in existing_columns:
                log.info("添加主键列 id...")
                cursor.execute(
                    f'ALTER TABLE "{safe_table}" ADD COLUMN IF NOT EXISTS '
                    f'id SERIAL PRIMARY KEY'
                )

            for col in data.keys():
                if col not in existing_columns:
                    col_type = "TIMESTAMPTZ" if col == "timestamp" \
                        else "TEXT" if col in ("serial", "model") \
                        else "JSONB"
                    log.info(f"添加缺失字段: {col} ({col_type})")
                    cursor.execute(
                        f'ALTER TABLE "{safe_table}" ADD COLUMN IF NOT EXISTS '
                        f'"{col}" {col_type}'
                    )

        columns = list(data.keys())
        col_string = ", ".join(f'"{col}"' for col in columns)
        placeholders = ", ".join(["%s"] * len(columns))
        values = [data[col] for col in columns]

        cursor.execute(f'''
            INSERT INTO "{safe_table}" ({col_string}) VALUES ({placeholders})
        ''', values)

        conn.commit()
        log.info(f"已写入数据库: serial={data.get('serial')}")
    except Exception as e:
        log.error(f"数据库操作失败: {e}")
        if conn:
            conn.rollback()
    finally:
        if cursor:
            cursor.close()
        if conn:
            conn.close()


def main():
    cfg = load_yaml(CONFIG_FILE)
    resolve_ssh_paths(cfg["ssh"], SCRIPT_DIR)
    resolve_pg_paths(cfg["postgres"], SCRIPT_DIR)

    ssh = connect_ssh(cfg["ssh"])
    try:
        log.info("开始新一轮 SMART 信息采集...")

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
                push_to_postgres(cfg, data)
            else:
                log.warning(f"SMART JSON 解析失败: {dev['cmd']}")
    finally:
        ssh.close()


if __name__ == "__main__":
    main()
