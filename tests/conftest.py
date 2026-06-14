import os
import socket
import subprocess
import sys
import tempfile
import time
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

PODMAN = os.environ.get("PODMAN_BIN", "podman")
PG_IMAGE = os.environ.get("PG_TEST_IMAGE", "docker.io/library/postgres:18")
ALPINE_IMAGE = os.environ.get("ALPINE_TEST_IMAGE", "docker.io/library/alpine:3.21")
MIRROR = os.environ.get("APK_MIRROR", "mirrors.tuna.tsinghua.edu.cn")
SSH_PORT_BASE = int(os.environ.get("SSH_TEST_PORT_BASE", "22022"))
PG_PORT = int(os.environ.get("PG_TEST_PORT", "25432"))


def _free_port():
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("", 0))
        return s.getsockname()[1]


def _podman(*args, **kwargs):
    return subprocess.run([PODMAN, *args], check=True, capture_output=True, text=True, **kwargs)


def _podman_check(*args):
    return subprocess.run([PODMAN, *args], capture_output=True, text=True)


def _wait_tcp(host, port, timeout=60):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            with socket.create_connection((host, port), timeout=2):
                return
        except OSError:
            time.sleep(1)
    raise TimeoutError(f"{host}:{port} 未在 {timeout}s 内就绪")


# ── session-scoped fixtures ──────────────────────────────────────────


@pytest.fixture(scope="session")
def podman_network():
    name = f"pytest_net_{os.getpid()}"
    r = _podman_check("network", "exists", name)
    if r.returncode == 0:
        _podman("network", "rm", "-f", name)
    _podman("network", "create", name)
    yield name
    _podman_check("network", "rm", "-f", name)


@pytest.fixture(scope="session")
def pg_container(podman_network):
    pg_pass = "test_pg_pass"
    pg_user = "testuser"
    pg_db = "testdb"
    container = f"pg_test_{os.getpid()}"
    port = _free_port()

    _podman_check("rm", "-f", container)
    _podman(
        "run", "-d", "--name", container,
        "--network", podman_network,
        "-p", f"{port}:5432",
        "-e", f"POSTGRES_USER={pg_user}",
        "-e", f"POSTGRES_DB={pg_db}",
        "-e", f"POSTGRES_PASSWORD={pg_pass}",
        "-e", "POSTGRES_INITDB_ARGS=--auth-host=scram-sha-256 --auth-local=scram-sha-256",
        PG_IMAGE,
    )
    try:
        _wait_tcp("127.0.0.1", port, timeout=60)
    except TimeoutError:
        _podman_check("logs", container)
        _podman_check("rm", "-f", container)
        raise

    conn_info = {
        "host": "127.0.0.1",
        "port": port,
        "user": pg_user,
        "password": pg_pass,
        "dbname": pg_db,
    }
    yield conn_info
    _podman_check("rm", "-f", container)


@pytest.fixture(scope="session")
def ssh_container(podman_network, tmp_path_factory):
    container = f"ssh_test_{os.getpid()}"
    port = _free_port()
    ssh_user = "testuser"
    ssh_pass = "test_ssh_pass"
    workdir = tmp_path_factory.mktemp("ssh_keys")
    key_path = workdir / "id_ed25519"
    pubkey_path = workdir / "id_ed25519.pub"

    from cryptography.hazmat.primitives.asymmetric import ed25519
    from cryptography.hazmat.primitives import serialization

    private_key = ed25519.Ed25519PrivateKey.generate()
    priv_pem = private_key.private_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PrivateFormat.OpenSSH,
        encryption=serialization.NoEncryption(),
    )
    key_path.write_bytes(priv_pem)
    key_path.chmod(0o600)

    public_key = private_key.public_key()
    pub_bytes = public_key.public_bytes(
        encoding=serialization.Encoding.OpenSSH,
        format=serialization.PublicFormat.OpenSSH,
    )
    pubkey_path.write_bytes(pub_bytes)

    setup_sh = workdir / "setup.sh"
    setup_sh.write_text(f"""#!/bin/sh
set -e
sed -i 's|dl-cdn.alpinelinux.org|{MIRROR}|' /etc/apk/repositories
apk add --no-cache openssh-server shadow

echo "{ssh_user}:{ssh_pass}" | chpasswd

mkdir -p /home/{ssh_user}/.ssh
cat > /home/{ssh_user}/.ssh/authorized_keys <<'EOFKEY'
{pub_bytes.decode()}
EOFKEY
chmod 700 /home/{ssh_user}/.ssh
chmod 600 /home/{ssh_user}/.ssh/authorized_keys
chown -R {ssh_user}:{ssh_user} /home/{ssh_user}

ssh-keygen -A

echo 'PasswordAuthentication yes' >> /etc/ssh/sshd_config
echo 'PermitRootLogin no' >> /etc/ssh/sshd_config
echo 'PubkeyAuthentication yes' >> /etc/ssh/sshd_config
""")
    setup_sh.chmod(0o755)

    _podman_check("rm", "-f", container)
    _podman(
        "run", "-d", "--name", container,
        "--network", podman_network,
        "-p", f"{port}:22",
        ALPINE_IMAGE,
        "sleep", "infinity",
    )

    _podman("cp", str(setup_sh), f"{container}:/setup.sh")
    _podman("exec", container, "/setup.sh")
    _podman("exec", container, "/usr/sbin/sshd", "-D", "-e")

    try:
        _wait_tcp("127.0.0.1", port, timeout=90)
    except TimeoutError:
        _podman_check("logs", container)
        _podman_check("rm", "-f", container)
        raise

    yield {
        "host": "127.0.0.1",
        "port": port,
        "user": ssh_user,
        "password": ssh_pass,
        "key_path": str(key_path),
        "pubkey": pub_bytes.decode(),
    }
    _podman_check("rm", "-f", container)


# ── function-scoped fixtures ─────────────────────────────────────────


@pytest.fixture
def pg_conn(pg_container):
    import psycopg2
    conn = psycopg2.connect(
        host=pg_container["host"],
        port=pg_container["port"],
        user=pg_container["user"],
        password=pg_container["password"],
        dbname=pg_container["dbname"],
    )
    conn.autocommit = True
    yield conn
    conn.close()


@pytest.fixture
def ssh_client(ssh_container):
    from lib.ssh_utils import connect_ssh
    cfg = {
        "host": ssh_container["host"],
        "port": ssh_container["port"],
        "user": ssh_container["user"],
        "password": ssh_container["password"],
        "key": ssh_container["key_path"],
        "known_host_key": f"{ssh_container['host']} ssh-ed25519 {ssh_container['pubkey'].split()[1]}",
    }
    client = connect_ssh(cfg)
    yield client
    try:
        client.close()
    except Exception:
        pass
