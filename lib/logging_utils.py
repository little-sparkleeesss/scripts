import logging

LOG_FORMAT = "[%(asctime)s] %(message)s"
LOG_DATE_FORMAT = "%Y-%m-%d %H:%M:%S"

_configured = False


def setup_logger(name=None, level=logging.INFO):
    global _configured
    if not _configured:
        logging.basicConfig(level=level, format=LOG_FORMAT, datefmt=LOG_DATE_FORMAT)
        _configured = True
    elif level != logging.root.level:
        logging.root.setLevel(level)
    return logging.getLogger(name)
