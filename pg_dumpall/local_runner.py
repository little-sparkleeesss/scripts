import os
import subprocess
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from lib.logging_utils import setup_logger

logger = setup_logger("PluggableBackup")


def main():
    current_dir = os.path.dirname(os.path.abspath(__file__))
    script_path = os.path.join(current_dir, "pg_full_backup.sh")

    logger.info("=" * 40 + " PostgreSQL 动态工具链备份启动 " + "=" * 40)

    if not os.path.exists(script_path):
        logger.error(f"核心脚本缺失: {script_path}")
        sys.exit(1)

    try:
        process = subprocess.Popen(
            ["bash", script_path],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            env=os.environ.copy(),
        )
        stdout, stderr = process.communicate()

        for line in stderr.decode("utf-8", errors="ignore").splitlines():
            logger.error(f"[STDERR] {line}")

        for line in stdout.decode("utf-8", errors="ignore").splitlines():
            logger.info(f"[STDOUT] {line}")

        if process.returncode == 0:
            logger.info("所有流水线阶段执行成功。")
        else:
            logger.error(f"流水线在某一阶段熔断，退出码: {process.returncode}")
            sys.exit(process.returncode)

    except Exception as e:
        logger.error(f"Runner 异常: {str(e)}")
        sys.exit(1)
    finally:
        logger.info("=" * 100)


if __name__ == "__main__":
    main()
