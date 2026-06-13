import os
import yaml


class ConfigError(Exception):
    pass


def load_yaml(path):
    try:
        with open(path, "r", encoding="utf-8") as f:
            config = yaml.safe_load(f)
    except FileNotFoundError:
        raise ConfigError(f"配置文件不存在: {path}")
    except yaml.YAMLError as e:
        raise ConfigError(f"YAML 解析失败: {path} — {e}")

    if config is None:
        raise ConfigError(f"配置文件为空: {path}")
    if not isinstance(config, dict):
        raise ConfigError(f"配置文件顶层必须是字典: {path}")

    return config


def resolve_path(base_dir, relative_path):
    if os.path.isabs(relative_path):
        return relative_path
    return os.path.normpath(os.path.join(base_dir, relative_path))


def script_dir(caller_file):
    return os.path.dirname(os.path.abspath(caller_file))


def resolve_ssh_paths(ssh_cfg, base_dir):
    for k in ("private_key", "key"):
        if ssh_cfg.get(k):
            ssh_cfg[k] = resolve_path(base_dir, ssh_cfg[k])
    return ssh_cfg


def resolve_pg_paths(pg_cfg, base_dir):
    if pg_cfg.get("ssl_root_cert"):
        pg_cfg["ssl_root_cert"] = resolve_path(base_dir, pg_cfg["ssl_root_cert"])
    mtls = pg_cfg.get("mtls", {})
    for k in ("client_cert", "client_key"):
        if mtls.get(k):
            mtls[k] = resolve_path(base_dir, mtls[k])
    return pg_cfg
