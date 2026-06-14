import os
import tempfile

import pytest

from lib.ssh_utils import (
    connect_ssh,
    open_sftp,
    run_remote_cmd,
    sftp_get_file,
    sftp_put_file,
    _parse_known_host_key,
)


class TestParseKnownHostKey:
    def test_ed25519_2_fields(self):
        line = "ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAIBOGvqDlcJ7gXQKmwWmBjKaKm0nBVLz"
        pkey = _parse_known_host_key(line)
        assert pkey is not None

    def test_ed25519_3_fields(self):
        line = "host.example.com ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAIBOGvqDlcJ7gXQKmwWmBjKaKm0nBVLz"
        pkey = _parse_known_host_key(line)
        assert pkey is not None

    def test_raises_on_empty(self):
        with pytest.raises(ValueError):
            _parse_known_host_key("")


class TestConnectSSH:
    def test_connects_with_password(self, ssh_container):
        cfg = {
            "host": ssh_container["host"],
            "port": ssh_container["port"],
            "user": ssh_container["user"],
            "password": ssh_container["password"],
            "known_host_key": ssh_container["host_key"],
        }
        client = connect_ssh(cfg)
        try:
            _, stdout, _ = client.exec_command("echo ok")
            assert stdout.read().decode().strip() == "ok"
        finally:
            client.close()

    def test_connects_with_key(self, ssh_container):
        cfg = {
            "host": ssh_container["host"],
            "port": ssh_container["port"],
            "user": ssh_container["user"],
            "key": ssh_container["key_path"],
            "known_host_key": ssh_container["host_key"],
        }
        client = connect_ssh(cfg)
        try:
            _, stdout, _ = client.exec_command("whoami")
            assert stdout.read().decode().strip() == ssh_container["user"]
        finally:
            client.close()

    def test_rejects_unknown_host(self, ssh_container):
        cfg = {
            "host": ssh_container["host"],
            "port": ssh_container["port"],
            "user": ssh_container["user"],
            "password": ssh_container["password"],
            "known_host_key": "evil.example.com ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAIGfakekeythatwillnevermatch123",
        }
        with pytest.raises(Exception):
            connect_ssh(cfg)


class TestRunRemoteCmd:
    def test_echo(self, ssh_client):
        result = run_remote_cmd(ssh_client, "echo hello")
        assert result.strip() == "hello"

    def test_nonzero_exit_raises(self, ssh_client):
        with pytest.raises(RuntimeError):
            run_remote_cmd(ssh_client, "exit 42")


class TestSftp:
    def test_put_and_get(self, ssh_client, tmp_path):
        local_file = tmp_path / "upload.txt"
        local_file.write_text("sfpt content")

        remote_path = "/tmp/pytest_sftp_test.dat"
        download_path = tmp_path / "download.txt"

        sftp_put_file(ssh_client, str(local_file), remote_path)

        sftp_get_file(ssh_client, remote_path, str(download_path))
        assert download_path.read_text() == "sfpt content"

        # cleanup remote
        sftp = open_sftp(ssh_client)
        try:
            sftp.remove(remote_path)
        finally:
            sftp.close()
