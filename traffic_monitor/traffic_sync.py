import os
import sys
import sqlite3
from datetime import datetime, timedelta, timezone

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from lib.config_utils import load_yaml, resolve_pg_paths, resolve_ssh_paths, script_dir
from lib.logging_utils import setup_logger
from lib.pg_utils import connect_postgres
from lib.ssh_utils import connect_ssh, run_remote_cmd, sftp_get_file

log = setup_logger("traffic_sync")

SCRIPT_DIR = script_dir(__file__)
CONFIG_FILE = os.path.join(SCRIPT_DIR, "config.yaml")
SQLITE_LOCAL = "/tmp/vnstat.db"


def fetch_sqlite(cfg):
    ssh_cfg = cfg["sftp"]
    ssh = connect_ssh(ssh_cfg)
    try:
        output = run_remote_cmd(ssh, "date +%z")
        tz_str = output.strip()
        if not tz_str or len(tz_str) != 5 or not tz_str[1:].isdigit():
            raise Exception(f"无法解析远程时区偏移: '{tz_str}'")
        sign = 1 if tz_str.startswith('+') else -1
        hours_offset = int(tz_str[1:3])
        minutes_offset = int(tz_str[3:])
        tz_offset = timezone(
            sign * timedelta(hours=hours_offset, minutes=minutes_offset)
        )
        log.info(f"远程主机时区偏移: {tz_str} ({tz_offset})")

        sftp_get_file(ssh, ssh_cfg["remote_path"], SQLITE_LOCAL)
        log.info("SQLite 文件拉取成功")
    finally:
        ssh.close()

    return tz_offset


def quote_identifier(identifier):
    return f'"{identifier}"'


def sync_data(cfg, tz_offset):
    sqlite_conn = None
    pg_conn = None
    pg_cursor = None
    try:
        sqlite_conn = sqlite3.connect(SQLITE_LOCAL)
        sqlite_cursor = sqlite_conn.cursor()
        log.info("打开 SQLite 成功")

        pg_cfg = cfg["postgres"]
        pg_conn = connect_postgres(pg_cfg)
        pg_cursor = pg_conn.cursor()

        pg_table = quote_identifier(pg_cfg["table"])
        sqlite_table = quote_identifier(cfg["sync"]["sqlite_table"])

        sync_columns = cfg["sync"]["columns"]
        column_mapping = cfg["sync"].get("column_mapping", {})

        sqlite_pk_name = cfg["sync"]["primary_key"]

        try:
            id_index = sync_columns.index(sqlite_pk_name)
        except ValueError:
            raise Exception(f"sync_columns 中缺少 primary_key: {sqlite_pk_name}")

        ts_index = -1
        pg_ts_col_name = "timestamp"
        for i, col in enumerate(sync_columns):
            mapped_name = column_mapping.get(col, col)
            if mapped_name == "timestamp" or col == "timestamp":
                ts_index = i
                pg_ts_col_name = mapped_name
                break

        if ts_index == -1:
            raise Exception("无法在配置中找到 timestamp 字段，无法执行增量同步")

        pg_pk_name = column_mapping.get(sqlite_pk_name, sqlite_pk_name)
        pg_columns = [column_mapping.get(col, col) for col in sync_columns]

        pg_cursor.execute(
            f'SELECT MAX("{pg_pk_name}"), MAX("{pg_ts_col_name}") FROM {pg_table}'
        )
        row = pg_cursor.fetchone()
        current_max_id = row[0] if row[0] is not None else 0
        last_sync_ts = row[1]

        log.info(
            f"PostgreSQL 当前最大 ID: {current_max_id}, 最后同步时间: {last_sync_ts}"
        )

        sqlite_cursor.execute(
            f"SELECT {', '.join(sync_columns)} FROM {sqlite_table} "
            f"ORDER BY {sync_columns[ts_index]} ASC"
        )
        sqlite_rows = sqlite_cursor.fetchall()

        insert_rows = []
        next_id = current_max_id + 1

        for row in sqlite_rows:
            row_list = list(row)

            ts_str = row_list[ts_index]
            try:
                dt = datetime.strptime(ts_str, "%Y-%m-%d %H:%M:%S")
                dt = dt.replace(tzinfo=tz_offset)
            except Exception as e:
                log.warning(f"跳过非法时间格式: {ts_str} ({e})")
                continue

            if last_sync_ts is None or dt > last_sync_ts:
                row_list[id_index] = next_id
                next_id += 1
                row_list[ts_index] = dt
                insert_rows.append(row_list)

        if not insert_rows:
            log.info("没有比当前数据库更新的数据，无需同步")
        else:
            log.info(f"发现 {len(insert_rows)} 条新记录，准备同步...")

            placeholders = ', '.join(['%s'] * len(pg_columns))
            insert_sql = (
                f"INSERT INTO {pg_table} ("
                + ", ".join(f'"{col}"' for col in pg_columns)
                + f") VALUES ({placeholders})"
            )

            try:
                pg_cursor.executemany(insert_sql, insert_rows)
                pg_conn.commit()
                log.info(
                    f"成功同步 {len(insert_rows)} 条记录，"
                    f"当前最大 ID 更新为 {next_id - 1}"
                )
            except Exception as e:
                pg_conn.rollback()
                log.error(f"写入数据库失败: {e}")
    finally:
        if sqlite_conn:
            sqlite_conn.close()
        if pg_cursor:
            pg_cursor.close()
        if pg_conn:
            pg_conn.close()
        if os.path.exists(SQLITE_LOCAL):
            os.remove(SQLITE_LOCAL)
            log.info("本地 SQLite 文件已删除")


def main():
    cfg = load_yaml(CONFIG_FILE)
    resolve_ssh_paths(cfg["sftp"], SCRIPT_DIR)
    resolve_pg_paths(cfg["postgres"], SCRIPT_DIR)

    try:
        tz_offset = fetch_sqlite(cfg)
        sync_data(cfg, tz_offset)
    except Exception as e:
        log.error(f"同步过程失败: {e}")
        sys.exit(1)


if __name__ == "__main__":
    main()
