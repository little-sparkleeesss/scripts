import base64
import logging
import os
import re
import time

import paramiko

log = logging.getLogger(__name__)

_PGPASSWORD_RE = re.compile(r"(PGPASSWORD=)('[^']*'|\S+)", re.ASCII)


def _parse_known_host_key(known_key_line):
    fields = known_key_line.split()
    if len(fields) < 3:
        raise ValueError(f"known_host_key 格式无效: {known_key_line!r}")
    host, key_type, key_b64 = fields[0], fields[1], fields[2]
    key_data = base64.b64decode(key_b64)

    if key_type == "ssh-ed25519":
        return paramiko.Ed25519Key(data=key_data)
    elif key_type == "ssh-rsa":
        return paramiko.RSAKey(data=key_data)
    elif key_type.startswith("ecdsa"):
        return paramiko.ECDSAKey(data=key_data)
    else:
        raise ValueError(f"不支持的 SSH 公钥类型: {key_type}")


def _load_private_key(key_file, passphrase=None):
    for factory in (
        paramiko.RSAKey,
        paramiko.Ed25519Key,
        paramiko.ECDSAKey,
    ):
        try:
            return factory.from_private_key_file(key_file, password=passphrase or None)
        except paramiko.SSHException:
            continue
    raise ValueError(f"无法读取私钥文件: {key_file}")


def connect_ssh(ssh_cfg, max_retries=3, retry_delay=5):
    host = ssh_cfg["host"]
    port = ssh_cfg.get("port", 22)
    username = ssh_cfg.get("username") or ssh_cfg.get("user")
    password = ssh_cfg.get("password") or None
    key_path = ssh_cfg.get("private_key") or ssh_cfg.get("key")
    key_passphrase = ssh_cfg.get("private_key_passphrase")
    known_host_line = ssh_cfg.get("known_host_key")

    if not username:
        raise ValueError("SSH 配置缺少 username/user")
    if not known_host_line:
        raise ValueError("SSH 配置缺少 known_host_key，拒绝连接以避免 MITM 风险")

    client = paramiko.SSHClient()
    client.set_missing_host_key_policy(paramiko.RejectPolicy())

    host_key = _parse_known_host_key(known_host_line)
    client.get_host_keys().add(host, host_key.get_name(), host_key)
    if port != 22:
        client.get_host_keys().add(f"[{host}]:{port}", host_key.get_name(), host_key)

    pkey = None
    if key_path:
        pkey = _load_private_key(key_path, key_passphrase)

    log.info(f"连接 SSH: {username}@{host}:{port} ...")

    for attempt in range(1, max_retries + 1):
        try:
            client.connect(
                hostname=host,
                port=port,
                username=username,
                pkey=pkey,
                password=password,
                timeout=10,
                look_for_keys=False,
                allow_agent=False,
            )
            log.info("SSH 连接成功 (已知主机验证通过)")
            return client
        except Exception as e:
            log.warning(f"SSH 连接失败 (第 {attempt}/{max_retries} 次): {e}")
            if attempt < max_retries:
                time.sleep(retry_delay)

    client.close()
    raise RuntimeError(f"SSH 连接失败，已重试 {max_retries} 次")


def open_sftp(ssh_client):
    return ssh_client.open_sftp()


def run_remote_cmd(ssh_client, cmd, timeout=300):
    safe_cmd = _PGPASSWORD_RE.sub(r"\1***", cmd)
    log.info(f"远程执行: {safe_cmd}")
    stdin, stdout, stderr = ssh_client.exec_command(cmd, timeout=timeout)
    stdout_text = stdout.read().decode()
    stderr_text = stderr.read().decode()
    exit_code = stdout.channel.recv_exit_status()
    if exit_code != 0:
        msg = f"远程命令失败 (exit={exit_code})"
        if stderr_text:
            msg += f"\n{stderr_text.rstrip()}"
        log.error(msg)
        raise RuntimeError(msg)
    return stdout_text


MAX_SFTP_SIZE = 512 * 1024 * 1024  # 512 MiB


def sftp_get_file(ssh_client, remote_path, local_path, max_size=MAX_SFTP_SIZE):
    sftp = open_sftp(ssh_client)
    try:
        stat = sftp.stat(remote_path)
        if stat.st_size > max_size:
            raise RuntimeError(
                f"SFTP 文件过大 ({stat.st_size} > {max_size} bytes): {remote_path}"
            )
        sftp.get(remote_path, local_path)
        log.info(f"SFTP 下载: {remote_path} -> {local_path}")
    finally:
        sftp.close()


def sftp_put_file(ssh_client, local_path, remote_path, max_size=MAX_SFTP_SIZE):
    local_size = os.path.getsize(local_path)
    if local_size > max_size:
        raise RuntimeError(
            f"本地文件过大 ({local_size} > {max_size} bytes): {local_path}"
        )
    sftp = open_sftp(ssh_client)
    try:
        sftp.put(local_path, remote_path)
        log.info(f"SFTP 上传: {local_path} -> {remote_path}")
    finally:
        sftp.close()
