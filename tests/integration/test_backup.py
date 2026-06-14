import time

import pytest
import psycopg2


class TestPgBackupStartStop:
    def test_backup_start_stop_same_session(self, pg_conn):
        cur = pg_conn.cursor()

        cur.execute("SELECT pg_backup_start('pytest_backup', true)")
        row = cur.fetchone()
        assert row is not None

        time.sleep(1)

        cur.execute("SELECT pg_backup_stop()")
        row = cur.fetchone()
        cur.close()

    def test_conn_survives_sleep(self, pg_conn):
        cur = pg_conn.cursor()
        cur.execute("SELECT pg_backup_start('pytest_sleep', true)")

        time.sleep(3)

        cur.execute("SELECT 1")
        assert cur.fetchone() == (1,)

        cur.execute("SELECT pg_backup_stop()")
        cur.close()

    def test_cannot_start_twice_without_stop(self, pg_conn):
        cur = pg_conn.cursor()
        cur.execute("SELECT pg_backup_start('pytest_double', true)")
        try:
            with pytest.raises(psycopg2.Error):
                cur.execute("SELECT pg_backup_start('pytest_double_again', true)")
        finally:
            cur.execute("SELECT pg_backup_stop()")
            cur.close()

    def test_ssl_check_query(self, pg_conn):
        cur = pg_conn.cursor()
        cur.execute("SELECT inet_server_addr(), inet_server_port()")
        addr, port = cur.fetchone()
        assert addr is not None
        assert port == 5432

        cur.execute(
            "SELECT ssl, version, cipher FROM pg_stat_ssl WHERE pid = pg_backend_pid()"
        )
        row = cur.fetchone()
        assert row is not None
        cur.close()
