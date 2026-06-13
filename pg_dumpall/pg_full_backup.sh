#!/bin/bash
set -euo pipefail

safe_source() {
    local conf="$1"
    local line
    local quoted_re='^[A-Z_][A-Z0-9_]*="[^"]*"$'
    local unquoted_re='^[A-Z_][A-Z0-9_]*=[^"`$()|&;<>]*$'
    while IFS= read -r line || [ -n "$line" ]; do
        [[ -z "${line}" || "${line}" =~ ^[[:space:]]*# ]] && continue
        if [[ "${line}" =~ ${quoted_re} ]]; then
            declare "${line}"
        elif [[ "${line}" =~ ${unquoted_re} ]]; then
            declare "${line}"
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
for _var in PG_BIN_DIR DB_SSL_CA NAS_PRIVATE_KEY; do
    _val="${!_var:-}"
    if [ -n "${_val}" ] && [ "${_val#/}" = "${_val}" ]; then
        declare "$_var=${SCRIPT_DIR}/${_val}"
    fi
done

# -------------------------- 1. 工具路径动态解析 --------------------------
get_binary() {
    local cmd=$1
    local custom_dir=$2
    if [ -n "${custom_dir}" ] && [ -x "${custom_dir}/${cmd}" ]; then
        echo "${custom_dir}/${cmd}"
    elif command -v "$cmd" &> /dev/null; then
        command -v "$cmd"
    else
        echo "ERROR: 未找到工具: ${cmd}" >&2; exit 1
    fi
}

PG_DUMPALL=$(get_binary "pg_dumpall" "${PG_BIN_DIR:-}")
ZSTD=$(get_binary "zstd" "${ZSTD_BIN_DIR:-}")
SCP=$(command -v scp)

# -------------------------- 2. 执行数据库备份 --------------------------
backup_file="${LOCAL_STAGING_DIR}/${BACKUP_PREFIX}_$(date +%Y%m%d_%H%M%S).sql.zst"
mkdir -p "${LOCAL_STAGING_DIR}"

if [ "${DB_SSL_ENABLE,,}" = "true" ]; then
    export PGSSLMODE="verify-full"
    export PGSSLROOTCERT="${DB_SSL_CA}"
    [ "${DB_MTLS_ENABLE,,}" = "true" ] && { export PGSSLCERT="${DB_MTLS_CERT}"; export PGSSLKEY="${DB_MTLS_KEY}"; }
fi

echo "INFO: [阶段1/2] 正在提取数据库流..."
[ -n "${DB_PASS:-}" ] && export PGPASSWORD="${DB_PASS}"

echo "INFO: 正在通过 psql 确认 monitor 用户的 SSL 状态..."

# 要求报告 SSL 情况 (set -e 下用 || true 防止 SSL 验证失败中断整个备份)
PSQL_BIN=$(get_binary "psql" "${PG_BIN_DIR:-}")
"$PSQL_BIN" -h "$DB_HOST" -p "$DB_PORT" -U "$DB_USER" -d postgres -c "\conninfo" || true

"$PSQL_BIN" -h "$DB_HOST" -p "$DB_PORT" -U "$DB_USER" -d postgres \
  -c "SELECT ssl, version, cipher FROM pg_stat_ssl WHERE pid = pg_backend_pid();" || true

if ! "$PG_DUMPALL" -h "${DB_HOST}" -p "${DB_PORT}" -U "${DB_USER}" --no-password \
    | "$ZSTD" -T0 --ultra -${COMPRESS_LEVEL} -o "${backup_file}"; then
    echo "ERROR: 备份失败！" >&2; exit 1
fi
unset PGPASSWORD

# -------------------------- 3. 安全推送到 NAS --------------------------
echo "INFO: [阶段2/2] 正在推送至 NAS (认证模式: ${NAS_AUTH_TYPE})..."

# 创建临时 known_hosts 抵御 MITM 攻击
TMP_KNOWN_HOSTS=$(mktemp)
echo "${NAS_HOST_KEY}" > "${TMP_KNOWN_HOSTS}"

# 基础 SCP 选项：指定端口、主机 Key 校验
SCP_OPTS="-P ${NAS_PORT} -o UserKnownHostsFile=${TMP_KNOWN_HOSTS} -o StrictHostKeyChecking=yes"

if [ "${NAS_AUTH_TYPE}" = "key" ]; then
    # --- 私钥模式 ---
    if [ ! -f "${NAS_PRIVATE_KEY}" ]; then
        echo "ERROR: 私钥文件不存在: ${NAS_PRIVATE_KEY}" >&2; exit 1
    fi
    # 强制设置私钥权限，否则 SSH 会拒绝使用
    chmod 600 "${NAS_PRIVATE_KEY}"

    echo "INFO: 使用私钥进行身份验证..."
    if "$SCP" ${SCP_OPTS} -i "${NAS_PRIVATE_KEY}" "${backup_file}" "${NAS_USER}@${NAS_HOST}:${NAS_REMOTE_DIR}/"; then
        TRANSFER_OK=true
    else
        TRANSFER_OK=false
    fi

elif [ "${NAS_AUTH_TYPE}" = "password" ]; then
    # --- 密码模式 ---
    if ! command -v sshpass &> /dev/null; then
        echo "ERROR: 密码模式需要安装 sshpass (apk add sshpass)" >&2; exit 1
    fi

    echo "INFO: 使用密码进行身份验证..."
    if SSHPASS="${NAS_PASS}" sshpass -e "$SCP" ${SCP_OPTS} "${backup_file}" "${NAS_USER}@${NAS_HOST}:${NAS_REMOTE_DIR}/"; then
        TRANSFER_OK=true
    else
        TRANSFER_OK=false
    fi
else
    echo "ERROR: 未知的 NAS_AUTH_TYPE: ${NAS_AUTH_TYPE}" >&2; exit 1
fi

# -------------------------- 4. 善后处理 --------------------------
rm -f "${TMP_KNOWN_HOSTS}"

if [ "${TRANSFER_OK}" = true ]; then
    echo "SUCCESS: 备份并上传成功，清理本地文件。"
    rm -f "${backup_file}"
else
    echo "ERROR: 传输失败！本地文件已删除。" >&2
    rm -f "${backup_file}"
    exit 1
fi
