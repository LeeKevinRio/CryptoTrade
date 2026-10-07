"""日誌系統"""

import logging
from pathlib import Path

_FMT = logging.Formatter(
    "[%(asctime)s] %(levelname)s %(name)s - %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
# 共用檔案 handler：main() 以 log_file 呼叫一次後，所有模組 logger（含先前已建立的）都寫入同一檔。
# 原本只掛在名為 "cryptotrade" 的 logger 上，而沒有任何模組用這個名字 → 紀錄檔永遠 0 KB，
# 停損單掛單失敗等 warning 只出現在主控台、事後查不到。
_file_handler: logging.Handler | None = None
_loggers: list[logging.Logger] = []


def setup_logger(name: str = "cryptotrade", level: str = "INFO", log_file: str | None = None) -> logging.Logger:
    global _file_handler
    logger = logging.getLogger(name)
    logger.setLevel(getattr(logging, level.upper(), logging.INFO))

    if not logger.handlers:
        ch = logging.StreamHandler()
        ch.setFormatter(_FMT)
        logger.addHandler(ch)
        _loggers.append(logger)

    if log_file and _file_handler is None:
        log_path = Path(log_file)
        log_path.parent.mkdir(parents=True, exist_ok=True)
        _file_handler = logging.FileHandler(log_file, encoding="utf-8")
        _file_handler.setFormatter(_FMT)
        for lg in _loggers:
            if _file_handler not in lg.handlers:
                lg.addHandler(_file_handler)

    if _file_handler is not None and _file_handler not in logger.handlers:
        logger.addHandler(_file_handler)

    return logger
