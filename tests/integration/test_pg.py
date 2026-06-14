import pytest
import psycopg2

from lib.pg_utils import build_connection_params, build_pg_env, build_pg_env_string, connect_postgres


class TestBuildConnectionParams:
    def test_minimal_params(self, pg_container):
        params = build_connection_params(pg_container)
        assert params["host"] == pg_container["host"]
        assert params["port"] == pg_container["port"]
        assert params["user"] == pg_container["user"]
        assert params["dbname"] == pg_container["dbname"]
        assert params["sslmode"] == "prefer"

    def test_password_included(self, pg_container):
        params = build_connection_params(pg_container)
        assert params["password"] == pg_container["password"]

    def test_dbname_alias_in_config(self):
        cfg = {"host": "h", "port": 5432, "user": "u", "password": "p", "database": "mydb"}
        params = build_connection_params(cfg)
        assert params["dbname"] == "mydb"

    def test_missing_dbname_raises(self):
        with pytest.raises(ValueError):
            build_connection_params({"host": "h", "port": 5432, "user": "u"})


class TestBuildPgEnv:
    def test_sets_env_vars(self, pg_container):
        env = build_pg_env(pg_container)
        assert env["PGHOST"] == str(pg_container["host"])
        assert env["PGPORT"] == str(pg_container["port"])
        assert env["PGDATABASE"] == pg_container["dbname"]


class TestBuildPgEnvString:
    def test_quotes_password(self):
        cfg = {"host": "h", "port": 5432, "user": "u", "password": "p w", "database": "db"}
        s = build_pg_env_string(cfg)
        assert "PGPASSWORD='p w'" in s


class TestConnectPostgres:
    def test_connects_and_queries(self, pg_container):
        conn = connect_postgres(pg_container)
        try:
            cur = conn.cursor()
            cur.execute("SELECT 1")
            assert cur.fetchone() == (1,)
            cur.close()
        finally:
            conn.close()

    def test_conn_autocommit(self, pg_container):
        conn = connect_postgres(pg_container)
        conn.autocommit = True
        try:
            cur = conn.cursor()
            cur.execute("CREATE TABLE IF NOT EXISTS _test_pg (x int)")
            conn.commit()
            cur.execute("INSERT INTO _test_pg VALUES (42)")
            cur.execute("SELECT x FROM _test_pg")
            assert cur.fetchone() == (42,)
            cur.execute("DROP TABLE _test_pg")
            cur.close()
        finally:
            conn.close()
