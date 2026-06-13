import os
import subprocess
import sys
import tempfile
import time

import psycopg2

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from lib.config_utils import load_yaml, resolve_pg_paths, script_dir
from lib.logging_utils import setup_logger
from lib.pg_utils import connect_postgres

log = setup_logger("pg_start_backup")

SCRIPT_DIR = script_dir(__file__)
CONFIG_FILE = os.path.join(SCRIPT_DIR, "config.yaml")

_KEEPALIVE_MS = 1000 * int(os.environ.get("PG_START_BACKUP_KEEPALIVE_MS", "60000"))


def _decrypt_key(encrypted_key_path, password):
    fd, tmp_key = tempfile.mkstemp(prefix="client_key_", suffix=".pem")
    os.close(fd)
    env = os.environ.copy()
    env["OPENSSL_PASSWORD"] = password
    for method in (["pkcs8"], ["rsa"]):
        try:
            result = subprocess.run(
                ["openssl", *method, "-in", encrypted_key_path,
                 "-passin", "env:OPENSSL_PASSWORD", "-out", tmp_key],
                env=env, capture_output=True, timeout=30,
            )
            if result.returncode == 0:
                os.chmod(tmp_key, 0o600)
                log.info(f"openssl {method[0]} 解密 client key 成功")
                return tmp_key
        except (subprocess.TimeoutExpired, FileNotFoundError):
            continue
    try:
        os.remove(tmp_key)
    except OSError:
        pass
    raise RuntimeError("openssl 解密 client key 失败，请检查密码或 key 类型")


def _cleanup_temp(files):
    for f in files:
        if f and os.path.isfile(f):
            try:
                os.remove(f)
            except OSError:
                pass


def main():
    log.info("=" * 40 + " PostgreSQL 备份前置流程启动 " + "=" * 40)

    config = load_yaml(CONFIG_FILE)
    pg_cfg = config["postgres"]
    pg_cfg = resolve_pg_paths(pg_cfg, SCRIPT_DIR)

    backup_label = str(config.get("backup_label", "scheduled_backup"))
    delay_seconds = int(config.get("delay_seconds", 600))

    temp_files = []

    try:
        mtls = pg_cfg.get("mtls", {})
        if mtls.get("enabled") and mtls.get("client_key_password"):
            decrypted = _decrypt_key(mtls["client_key"], mtls["client_key_password"])
            mtls["client_key"] = decrypted
            temp_files.append(decrypted)

        pg_cfg["keepalives"] = "1"
        pg_cfg["keepalives_idle"] = str(_KEEPALIVE_MS // 1000)
        pg_cfg["keepalives_interval"] = str(max(1, _KEEPALIVE_MS // 10000))
        pg_cfg["keepalives_count"] = "5"

        conn = connect_postgres(pg_cfg)
        conn.autocommit = True
        cur = conn.cursor()

        cur.execute("SELECT inet_server_addr(), inet_server_port()")
        addr, port = cur.fetchone()
        log.info(f"连接信息: {addr}:{port}")

        cur.execute(
            "SELECT ssl, version, cipher FROM pg_stat_ssl WHERE pid = pg_backend_pid()"
        )
        row = cur.fetchone()
        if row:
            ssl, ver, cipher = row
            log.info(f"SSL: ssl={ssl} version={ver} cipher={cipher}")

        log.info("开始执行 VACUUM ANALYZE...")
        cur.execute("VACUUM ANALYZE")
        log.info("VACUUM ANALYZE 完成")

        log.info(f"pg_backup_start('{backup_label}', true)")
        cur.execute("SELECT * FROM pg_backup_start(%s, true)", (backup_label,))
        row = cur.fetchone()
        log.info(f"备份模式已启动: {row}")

        log.info(f"等待 {delay_seconds}s ({delay_seconds // 60}min)...")
        time.sleep(delay_seconds)

        log.info("pg_backup_stop()")
        cur.execute("SELECT * FROM pg_backup_stop()")
        row = cur.fetchone()
        log.info(f"备份模式已终止: {row}")

        cur.close()
        conn.close()
        log.info("备份前置流程执行成功")

    except psycopg2.Error as e:
        log.error(f"PostgreSQL 操作失败: {e}")
        sys.exit(1)
    except KeyboardInterrupt:
        log.warning("收到中断信号，PostgreSQL 会在连接断开后自动终止备份模式")
        sys.exit(1)
    except Exception as e:
        log.error(f"流程异常: {e}")
        sys.exit(1)
    finally:
        _cleanup_temp(temp_files)
        log.info("=" * 100)


if __name__ == "__main__":
    main()
