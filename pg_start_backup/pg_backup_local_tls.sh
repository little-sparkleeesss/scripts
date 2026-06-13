#!/usr/bin/env bash
set -euo pipefail
IFS=$'\n\t'

safe_source() {
    local conf="$1"
    local line
    while IFS= read -r line || [ -n "$line" ]; do
        [[ -z "${line}" || "${line}" =~ ^[[:space:]]*# ]] && continue
        if [[ "${line}" =~ ^[A-Z_][A-Z0-9_]*=\"[^\"]*\"$ ]]; then
            declare "${line}"
        elif [[ "${line}" =~ ^[A-Z_][A-Z0-9_]*=[^\"\`\$\(\)\|\&\;<>]*$ ]]; then
            declare "${line}"
        else
            printf "ERROR: 配置文件包含不安全的行: %s\n" "${line}" >&2
            return 1
        fi
    done < "${conf}"
}

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
CONFIG_FILE="${SCRIPT_DIR}/pg_backup.conf"

log() { printf "[%s] %s\n" "$(date +%Y-%m-%d_%H:%M:%S)" "$*"; }

if [ ! -f "$CONFIG_FILE" ]; then
  echo "配置文件未找到：$CONFIG_FILE" >&2
  exit 1
fi
safe_source "$CONFIG_FILE"
log "已加载配置：$CONFIG_FILE"

# 解析配置中相对于 SCRIPT_DIR 的路径
for _var in PG_BIN CA_CERT; do
    _val="${!_var:-}"
    if [ -n "${_val}" ] && [ "${_val#/}" = "${_val}" ]; then
        declare "$_var=${SCRIPT_DIR}/${_val}"
    fi
done

# 基本默认
PG_USER=${PG_USER:-postgres}
PG_DB=${PG_DB:-postgres}
PG_PORT=${PG_PORT:-5432}
PG_HOST=${PG_HOST:-localhost}
BACKUP_LABEL=${BACKUP_LABEL:-"scheduled_backup"}
DELAY_MINUTES=${DELAY_MINUTES:-10}
DELAY_SECONDS=$((DELAY_MINUTES * 60))
BACKUP_LABEL_ESCAPED="${BACKUP_LABEL//\'/\'\'}"

# psql 二进制查找
if [ -n "${PG_BIN:-}" ] && [ -x "${PG_BIN}" ]; then
  : # 使用配置的 PG_BIN
else
  PG_BIN="$(command -v psql || true)"
  if [ -z "$PG_BIN" ]; then
    log "错误：未找到 psql，请检查 PostgreSQL 客户端是否安装，或在配置中设置 PG_BIN"
    exit 1
  fi
  log "自动找到 psql：$PG_BIN"
fi

# TLS 基本校验（当启用时）
USE_TLS=${USE_TLS:-"yes"}
SSL_MODE=${SSL_MODE:-"verify-full"}
CA_CERT=${CA_CERT:-""}
if [ "${USE_TLS}" = "yes" ]; then
  if [ -z "$CA_CERT" ] || [ ! -f "$CA_CERT" ]; then
    log "错误：已启用 TLS，但 CA_CERT 未配置或文件不存在（$CA_CERT）"
    exit 1
  fi
  log "TLS启用，sslmode=${SSL_MODE}，CA证书：${CA_CERT}"
fi

# mTLS（客户端证书）配置
USE_MTLS=${USE_MTLS:-"no"}  # yes/no
CLIENT_CERT=${CLIENT_CERT:-""}
CLIENT_KEY=${CLIENT_KEY:-""}
CLIENT_KEY_PASSWORD=${CLIENT_KEY_PASSWORD:-""}

TMP_PGPASS=""
TMP_CLIENT_KEY=""

cleanup() {
  # 删除临时 pgpass
  if [ -n "$TMP_PGPASS" ] && [ -f "$TMP_PGPASS" ]; then
    shred -u "$TMP_PGPASS" 2>/dev/null || rm -f "$TMP_PGPASS"
    log "已删除临时 pgpass 文件"
  fi
  # 删除临时解密后的 client key
  if [ -n "$TMP_CLIENT_KEY" ] && [ -f "$TMP_CLIENT_KEY" ]; then
    shred -u "$TMP_CLIENT_KEY" 2>/dev/null || rm -f "$TMP_CLIENT_KEY"
    log "已删除临时解密后的 client key"
  fi
}
trap cleanup EXIT

# 认证：PG_PASSWORD 或 PGPASS_FILE
if [ -n "${PG_PASSWORD:-}" ]; then
  TMP_PGPASS="$(mktemp -p /tmp pgpass.XXXXXX)"
  echo "${PG_HOST}:${PG_PORT}:*:${PG_USER}:${PG_PASSWORD}" > "$TMP_PGPASS"
  chmod 600 "$TMP_PGPASS"
  export PGPASSFILE="$TMP_PGPASS"
  log "使用临时 pgpass（权限600）进行认证"
elif [ -n "${PGPASS_FILE:-}" ] && [ -f "${PGPASS_FILE}" ]; then
  perm=$(stat -c "%a" "$PGPASS_FILE" 2>/dev/null || echo "000")
  if [ "$perm" != "600" ]; then
    log "警告：$PGPASS_FILE 权限为 $perm，建议设置为 600（PostgreSQL 可能会忽略不安全的文件）"
  fi
  export PGPASSFILE="$PGPASS_FILE"
  log "使用现有 pgpass：$PGPASS_FILE"
else
  log "错误：未提供 PG_PASSWORD 且未找到 PGPASS_FILE（${PGPASS_FILE:-}）。无法进行无人值守认证。"
  exit 1
fi

# 若启用 mTLS，则校验证书和私钥并确保私钥权限
if [ "${USE_MTLS}" = "yes" ]; then
  if [ -z "${CLIENT_CERT}" ] || [ ! -f "${CLIENT_CERT}" ]; then
    log "错误：启用 mTLS，但 CLIENT_CERT 未配置或文件不存在（${CLIENT_CERT}）"
    exit 1
  fi
  if [ -z "${CLIENT_KEY}" ] || [ ! -f "${CLIENT_KEY}" ]; then
    log "错误：启用 mTLS，但 CLIENT_KEY 未配置或文件不存在（${CLIENT_KEY}）"
    exit 1
  fi

  # 如果提供了 CLIENT_KEY_PASSWORD，尝试用 openssl 解密为临时未加密 key（支持 pkcs8 或 rsa 两种尝试）
  if [ -n "${CLIENT_KEY_PASSWORD:-}" ]; then
    if ! command -v openssl >/dev/null 2>&1; then
      log "错误：CLIENT_KEY_PASSWORD 已设置，但系统中未找到 openssl，无法解密私钥"
      exit 1
    fi
    TMP_CLIENT_KEY="$(mktemp -p /tmp client_key.XXXXXX)"
    # 先尝试 pkcs8 解密 (密码通过环境变量传入，避免出现在 ps 中)
    if OPENSSL_PASSWORD="${CLIENT_KEY_PASSWORD}" openssl pkcs8 -in "${CLIENT_KEY}" -passin env:OPENSSL_PASSWORD -out "${TMP_CLIENT_KEY}" 2>/dev/null; then
      log "使用 openssl pkcs8 解密 client key 成功（输出临时文件）"
    elif OPENSSL_PASSWORD="${CLIENT_KEY_PASSWORD}" openssl rsa -in "${CLIENT_KEY}" -passin env:OPENSSL_PASSWORD -out "${TMP_CLIENT_KEY}" 2>/dev/null; then
      log "使用 openssl rsa 解密 client key 成功（输出临时文件）"
    else
      rm -f "${TMP_CLIENT_KEY}" || true
      log "错误：尝试用 openssl 解密 CLIENT_KEY 失败（检查密码或 key 类型），请提供未加密的 key 或确认密码"
      exit 1
    fi
    chmod 600 "${TMP_CLIENT_KEY}"
    CLIENT_KEY="${TMP_CLIENT_KEY}"
  else
    # 确保 key 权限安全
    perm=$(stat -c "%a" "${CLIENT_KEY}" 2>/dev/null || echo "000")
    if [ "$perm" != "600" ]; then
      log "警告：CLIENT_KEY 权限为 ${perm}，建议设置为 600（脚本会尝试自动修正）"
      chmod 600 "${CLIENT_KEY}" || log "无法自动修改 CLIENT_KEY 权限，请手动设置为 600"
    fi
  fi
  log "mTLS 启用：client cert=${CLIENT_CERT} client key=${CLIENT_KEY}"
fi

# 构造 libpq 连接字符串（作为 psql 的第一个参数）
# 包含 TLS 参数（sslmode/sslrootcert）以及在 mTLS 情况下的 sslcert/sslkey
CONN="host=${PG_HOST} port=${PG_PORT} user=${PG_USER} dbname=${PG_DB}"
if [ "${USE_TLS}" = "yes" ]; then
  CONN="${CONN} sslmode=${SSL_MODE} sslrootcert=${CA_CERT}"
fi
if [ "${USE_MTLS}" = "yes" ]; then
  CONN="${CONN} sslcert=${CLIENT_CERT} sslkey=${CLIENT_KEY}"
fi

log "最终连接字符串示例（隐藏敏感信息）：host=${PG_HOST} port=${PG_PORT} user=${PG_USER} dbname=${PG_DB} sslmode=${SSL_MODE} $( [ "${USE_MTLS}" = "yes" ] && echo "mTLS=on" || true )"

"${PG_BIN}" "${CONN}" -c "\conninfo"

"${PG_BIN}" "${CONN}" \
  -c "SELECT ssl, version, cipher FROM pg_stat_ssl WHERE pid = pg_backend_pid();"

# 执行 VACUUM ANALYZE
log "开始执行 VACUUM ANALYZE..."
if ! "${PG_BIN}" "${CONN}" -c "VACUUM ANALYZE;" ; then
  log "错误：VACUUM ANALYZE 执行失败"
  exit 1
fi
log "VACUUM ANALYZE 完成"

# 在单个会话中执行 pg_backup_start / pg_sleep / pg_backup_stop（使用新版函数）
log "开始在单个会话中执行 pg_backup_start -> pg_sleep -> pg_backup_stop"
if "${PG_BIN}" -v ON_ERROR_STOP=1 "${CONN}" <<SQL; then
-- 新版本函数（仅支持新版）
SELECT pg_backup_start('${BACKUP_LABEL_ESCAPED}', true);
SELECT pg_sleep(${DELAY_SECONDS});
SELECT pg_backup_stop();
SQL
  log "备份前置流程（pg_backup_start -> sleep -> pg_backup_stop）执行成功"
else
  log "错误：备份流程执行失败"
  exit 1
fi

log "脚本执行完毕"
