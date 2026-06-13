#!/bin/bash
set -euo pipefail
trap 'echo "TRACE: line=${LINENO:-?} cmd=${BASH_COMMAND:-?} exit=${?}" >&2' ERR

safe_source() {
    local conf="$1"
    local line stripped key value
    local quoted_re='^[A-Z_][A-Z0-9_]*="[^"]*"$'
    local unquoted_re='^[A-Z_][A-Z0-9_]*=[^"`$()|&;<>]*$'
    while IFS= read -r line || [ -n "$line" ]; do
        stripped="${line%%#*}"
        stripped="${stripped%"${stripped##*[![:space:]]}"}"
        [[ -z "${stripped}" ]] && continue
        if [[ "${stripped}" =~ ${quoted_re} ]]; then
            key="${stripped%%=*}"
            value="${stripped#*=}"
            value="${value#\"}"
            value="${value%\"}"
            declare -g "${key}=${value}"
        elif [[ "${stripped}" =~ ${unquoted_re} ]]; then
            declare -g "${stripped}"
        else
            printf "ERROR: 配置文件包含不安全的行: %s\n" "${line}" >&2
            return 1
        fi
    done < "${conf}"
}

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
CONF_PATH="${SCRIPT_DIR}/pg_backup.conf"
[ -f "${CONF_PATH}" ] && safe_source "${CONF_PATH}" || { echo "ERROR: 配置文件不存在"; exit 1; }

# 解析配置中相对于 SCRIPT_DIR 的路径
for _var in PG_BIN_DIR ZSTD_BIN_DIR DB_SSL_CA; do
    _val="${!_var:-}"
    if [ -n "${_val}" ] && [ "${_val#/}" = "${_val}" ]; then
        declare -g "$_var=${SCRIPT_DIR}/${_val}"
    fi
done

# -------------------------- 1. 工具路径动态解析 --------------------------
get_binary() {
    local cmd=$1
    local custom_path=$2
    if [ -n "${custom_path}" ]; then
        if [ -f "${custom_path}" ] && [ -x "${custom_path}" ]; then
            echo "${custom_path}"
            return 0
        elif [ -x "${custom_path}/${cmd}" ]; then
            echo "${custom_path}/${cmd}"
            return 0
        fi
    fi
    command -v "$cmd" 2>/dev/null && return 0
    echo "ERROR: 未找到工具: ${cmd}" >&2; exit 1
}

PG_DUMPALL=$(get_binary "pg_dumpall" "${PG_BIN_DIR:-}")
ZSTD=$(get_binary "zstd" "${ZSTD_BIN_DIR:-}")

# -------------------------- 2. 执行数据库备份 --------------------------
backup_file="${LOCAL_STAGING_DIR}/${BACKUP_PREFIX}_$(date +%Y%m%d_%H%M%S).sql.zst"
mkdir -p "${LOCAL_STAGING_DIR}"

if [ "${DB_SSL_ENABLE,,}" = "true" ]; then
    export PGSSLMODE="verify-full"
    export PGSSLROOTCERT="${DB_SSL_CA}"
    [ "${DB_MTLS_ENABLE,,}" = "true" ] && { export PGSSLCERT="${DB_MTLS_CERT}"; export PGSSLKEY="${DB_MTLS_KEY}"; }
fi

echo "INFO: [阶段1/1] 正在提取数据库流..."
[ -n "${DB_PASS:-}" ] && export PGPASSWORD="${DB_PASS}"

echo "INFO: 正在通过 psql 确认 monitor 用户的 SSL 状态..."

# 要求报告 SSL 情况 (set -e 下用 || true 防止 SSL 验证失败中断整个备份)
PSQL_BIN=$(get_binary "psql" "${PG_BIN_DIR:-}")
"$PSQL_BIN" -h "$DB_HOST" -p "$DB_PORT" -U "$DB_USER" -d postgres -c "\conninfo" || true

"$PSQL_BIN" -h "$DB_HOST" -p "$DB_PORT" -U "$DB_USER" -d postgres \
  -c "SELECT ssl, version, cipher FROM pg_stat_ssl WHERE pid = pg_backend_pid();" || true

if ! "$PG_DUMPALL" -h "${DB_HOST}" -p "${DB_PORT}" -U "${DB_USER}" --no-password \
    | stdbuf -e0 "$ZSTD" -T0 --ultra -${COMPRESS_LEVEL} -o "${backup_file}"; then
    echo "ERROR: 备份失败！" >&2; exit 1
fi
unset PGPASSWORD

echo "BACKUP_FILE=${backup_file}"
