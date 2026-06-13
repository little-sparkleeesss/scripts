import logging
import os
import shlex

import psycopg2

log = logging.getLogger(__name__)


def _get_dbname(cfg):
    dbname = cfg.get("dbname") or cfg.get("database", "")
    if not dbname:
        raise ValueError("PG 配置缺少 dbname / database")
    return dbname


def _get_sslmode(cfg):
    return cfg.get("ssl_mode") or cfg.get("sslmode", "prefer")


def build_pg_env(pg_cfg):
    env = os.environ.copy()
    env["PGHOST"] = str(pg_cfg["host"])
    env["PGPORT"] = str(pg_cfg["port"])
    env["PGUSER"] = str(pg_cfg["user"])
    env["PGPASSWORD"] = str(pg_cfg.get("password", ""))
    env["PGDATABASE"] = str(_get_dbname(pg_cfg))
    env["PGSSLMODE"] = str(_get_sslmode(pg_cfg))
    return env


def build_pg_env_string(pg_cfg):
    password = shlex.quote(str(pg_cfg.get("password", "")))
    return (
        f"PGHOST={pg_cfg['host']} "
        f"PGPORT={pg_cfg['port']} "
        f"PGUSER={pg_cfg['user']} "
        f"PGPASSWORD={password} "
        f"PGDATABASE={_get_dbname(pg_cfg)} "
        f"PGSSLMODE={_get_sslmode(pg_cfg)} "
    )


def build_connection_params(pg_cfg):
    params = {
        "host": pg_cfg["host"],
        "port": pg_cfg["port"],
        "user": pg_cfg["user"],
        "password": pg_cfg.get("password", ""),
        "dbname": _get_dbname(pg_cfg),
        "sslmode": _get_sslmode(pg_cfg),
    }

    if "ssl_root_cert" in pg_cfg and pg_cfg["ssl_root_cert"]:
        params["sslrootcert"] = pg_cfg["ssl_root_cert"]
    elif "sslrootcert" in pg_cfg and pg_cfg["sslrootcert"]:
        params["sslrootcert"] = pg_cfg["sslrootcert"]

    mtls = pg_cfg.get("mtls", {})
    if mtls.get("enabled", False):
        if not mtls.get("client_cert") or not mtls.get("client_key"):
            raise ValueError("mTLS 已启用但缺少 client_cert 或 client_key")
        params["sslcert"] = mtls["client_cert"]
        params["sslkey"] = mtls["client_key"]
        log.info("已启用 mTLS 客户端证书认证")

    return params


def connect_postgres(pg_cfg):
    params = build_connection_params(pg_cfg)
    log.info(
        f"连接 PostgreSQL: {params['user']}@{params['host']}:{params['port']}/{params['dbname']}"
    )
    try:
        conn = psycopg2.connect(**params, connect_timeout=10)
        log.info("PostgreSQL 连接成功")
        return conn
    except psycopg2.Error as e:
        log.error(f"PostgreSQL 连接失败: {e}")
        raise
