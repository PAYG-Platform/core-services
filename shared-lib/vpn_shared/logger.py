# shared-lib/vpn_shared/logger.py
import os
import sys
import logging
from logging.handlers import RotatingFileHandler
from typing import Optional


def setup_logging(
    service_name: str,
    log_dir: Optional[str] = None,
    log_level: Optional[str] = None,
    max_bytes: int = 15 * 1024 * 1024,  # 15 MB per file
    backup_count: int = 7,              # Keep 7 rotated historical archives
) -> logging.Logger:
    """
    Standardized Enterprise Dual-Channel Logger:
    1. Outputs unbuffered to stdout for Docker log drivers and real-time streaming.
    2. Writes to an auto-rotating UTF-8 log file inside log_dir with max-size limits.
    3. Intercepts Uvicorn, FastStream, and third-party logs to prevent format fragmentation.
    """
    level_str = (log_level or os.getenv("LOG_LEVEL", "INFO")).upper().strip()
    level = getattr(logging, level_str, logging.INFO)

    log_format = "[%(asctime)s] [%(levelname)s] [%(name)s] [%(process)d] - %(message)s"
    date_format = "%Y-%m-%d %H:%M:%S"
    formatter = logging.Formatter(log_format, datefmt=date_format)

    # 1. Console Handler (stdout)
    console_handler = logging.StreamHandler(sys.stdout)
    console_handler.setLevel(level)
    console_handler.setFormatter(formatter)

    handlers = [console_handler]

    # 2. Rotating File Handler
    resolved_log_dir = log_dir or os.getenv("LOG_DIR", "./logs" if not os.path.exists("/app") else "/app/logs")
    try:
        os.makedirs(resolved_log_dir, exist_ok=True)
        log_file = os.path.join(resolved_log_dir, f"{service_name}.log")
        file_handler = RotatingFileHandler(
            filename=log_file,
            maxBytes=max_bytes,
            backupCount=backup_count,
            encoding="utf-8",
        )
        file_handler.setLevel(level)
        file_handler.setFormatter(formatter)
        handlers.append(file_handler)
    except Exception:
        # Fallback to local ./logs directory if /app/logs is not writable on host machine
        try:
            local_fallback = "./logs"
            os.makedirs(local_fallback, exist_ok=True)
            log_file = os.path.join(local_fallback, f"{service_name}.log")
            file_handler = RotatingFileHandler(
                filename=log_file,
                maxBytes=max_bytes,
                backupCount=backup_count,
                encoding="utf-8",
            )
            file_handler.setLevel(level)
            file_handler.setFormatter(formatter)
            handlers.append(file_handler)
        except Exception:
            pass

    # 3. Configure Root Logger
    root_logger = logging.getLogger()
    root_logger.setLevel(level)
    root_logger.handlers = handlers

    # 4. Clean Uvicorn & Third-Party Handlers to Prevent Duplicate Lines
    for lib_logger_name in [
        "uvicorn",
        "uvicorn.error",
        "uvicorn.access",
        "faststream",
        "faststream.rabbit",
        "apscheduler",
        "telethon",
    ]:
        lib_logger = logging.getLogger(lib_logger_name)
        lib_logger.handlers = []
        lib_logger.propagate = True

    service_logger = logging.getLogger(service_name)
    service_logger.info(
        f"Logging initialized for '{service_name}' [Level: {level_str}, File: {resolved_log_dir}/{service_name}.log]"
    )
    return service_logger