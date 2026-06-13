import os
import re
import subprocess
import sys
from queue import Empty, Queue
from threading import Thread

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from lib.logging_utils import setup_logger
from lib.ssh_utils import connect_ssh, sftp_put_file

logger = setup_logger("PluggableBackup")

BACKUP_FILE_RE = re.compile(r"BACKUP_FILE=(.+)")


def _parse_bash_config(conf_path):
    config = {}
    quoted_re = re.compile(r'^([A-Z_][A-Z0-9_]*)="([^"]*)"$')
    unquoted_re = re.compile(r'^([A-Z_][A-Z0-9_]*)=([^"]+)$')
    with open(conf_path, "r", encoding="utf-8") as f:
        for line in f:
            line = line.split("#")[0].strip()
            if not line:
                continue
            m = quoted_re.match(line) or unquoted_re.match(line)
            if m:
                config[m.group(1)] = m.group(2)
    return config


def _resolve_path(conf_dir, value):
    if value and not os.path.isabs(value):
        return os.path.normpath(os.path.join(conf_dir, value))
    return value


def _pipe_reader(pipe, tag, log_func, output_queue):
    for raw in pipe:
        for line in raw.decode("utf-8", errors="ignore").splitlines():
            output_queue.put((tag, log_func, line))
    pipe.close()


def main():
    current_dir = os.path.dirname(os.path.abspath(__file__))
    script_path = os.path.join(current_dir, "pg_full_backup.sh")
    conf_path = os.path.join(current_dir, "pg_backup.conf")

    logger.info("=" * 40 + " PostgreSQL 动态工具链备份启动 " + "=" * 40)

    if not os.path.exists(script_path):
        logger.error(f"核心脚本缺失: {script_path}")
        sys.exit(1)

    try:
        process = subprocess.Popen(
            ["bash", script_path],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            env=os.environ.copy(),
        )

        output_queue = Queue()
        threads = [
            Thread(target=_pipe_reader, args=(process.stdout, "STDOUT", logger.info, output_queue), daemon=True),
            Thread(target=_pipe_reader, args=(process.stderr, "STDERR", logger.error, output_queue), daemon=True),
        ]
        for t in threads:
            t.start()

        backup_file = None
        any_output = False
        while process.poll() is None:
            try:
                tag, log_func, line = output_queue.get(timeout=0.2)
                any_output = True
                log_func(f"[{tag}] {line}")
                if tag == "STDOUT":
                    m = BACKUP_FILE_RE.search(line)
                    if m:
                        backup_file = m.group(1).strip()
            except Empty:
                pass

        for t in threads:
            t.join(timeout=1)

        while not output_queue.empty():
            try:
                tag, log_func, line = output_queue.get_nowait()
                any_output = True
                log_func(f"[{tag}] {line}")
                if tag == "STDOUT":
                    m = BACKUP_FILE_RE.search(line)
                    if m:
                        backup_file = m.group(1).strip()
            except Empty:
                break

        if not any_output:
            logger.warning("子进程无任何输出 (stdout+stderr 均为空)")

        if process.returncode != 0:
            logger.error(f"流水线在某一阶段熔断，退出码: {process.returncode}")
            sys.exit(process.returncode)

        if not backup_file or not os.path.isfile(backup_file):
            logger.error(f"备份文件未生成或路径无效: {backup_file}")
            sys.exit(1)

        logger.info(f"备份文件已生成: {backup_file}")

        nas_cfg = _parse_bash_config(conf_path)
        nas_host = nas_cfg.get("NAS_HOST", "")
        if not nas_host or nas_host.startswith("<"):
            logger.info("未配置 NAS 目标，跳过远程推送，保留本地备份文件")
            return

        key_path = _resolve_path(current_dir, nas_cfg.get("NAS_PRIVATE_KEY", ""))
        if not os.path.isfile(key_path):
            key_path = None

        password = nas_cfg.get("NAS_PASS", "")
        if password.startswith("<"):
            password = None

        if not key_path and not password:
            logger.error("NAS 配置缺少有效的认证凭据 (私钥或密码)")
            sys.exit(1)

        known_host_key = nas_cfg.get("NAS_HOST_KEY", "")
        if not known_host_key or known_host_key.startswith("<"):
            logger.error("NAS 配置缺少 NAS_HOST_KEY，拒绝连接以避免 MITM 风险")
            sys.exit(1)

        ssh_cfg = {
            "host": nas_host,
            "port": int(nas_cfg.get("NAS_PORT", "22")),
            "username": nas_cfg.get("NAS_USER", ""),
            "password": password,
            "private_key": key_path,
            "known_host_key": known_host_key,
        }

        nas_remote_dir = nas_cfg.get("NAS_REMOTE_DIR", "").rstrip("/")
        if not nas_remote_dir:
            logger.error("NAS 配置缺少 NAS_REMOTE_DIR")
            sys.exit(1)

        remote_file = f"{nas_remote_dir}/{os.path.basename(backup_file)}"
        logger.info(f"正在推送至 NAS ({ssh_cfg['username']}@{ssh_cfg['host']}:{ssh_cfg['port']})...")

        client = connect_ssh(ssh_cfg)
        try:
            sftp_put_file(client, backup_file, remote_file)
            logger.info("备份推送成功")
        finally:
            client.close()

        os.remove(backup_file)
        logger.info("本地临时文件已清理")

    except Exception as e:
        logger.error(f"Runner 异常: {str(e)}")
        sys.exit(1)
    finally:
        logger.info("=" * 100)


if __name__ == "__main__":
    main()
