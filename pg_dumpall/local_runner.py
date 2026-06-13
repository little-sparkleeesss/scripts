import os
import select
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
            bufsize=0,
            env=os.environ.copy(),
        )

        while True:
            rlist, _, _ = select.select(
                [process.stdout, process.stderr], [], [], 0.5
            )
            for f in rlist:
                line = f.readline().decode("utf-8", errors="ignore")
                if line:
                    if f == process.stdout:
                        print(f"  [STDOUT] {line.strip()}")
                    else:
                        print(
                            f"\033[31m  [STDERR] {line.strip()}\033[0m",
                            file=sys.stderr,
                        )

            if process.poll() is not None:
                break

        for f in (process.stdout, process.stderr):
            for line in f.readlines():
                line = line.decode("utf-8", errors="ignore")
                if line:
                    print(f"  [OUTPUT] {line.strip()}")

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
