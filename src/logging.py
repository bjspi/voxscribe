"""
Logging module for the Telegram Voice Transcription Bot.

This module sets up the logging configuration for the bot, including
both file and console logging with appropriate formatting.
"""

import logging
import os
import re
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any

import yaml

PROJECT_ROOT = Path(__file__).resolve().parent.parent
CONFIG_FILE = PROJECT_ROOT / "config.yaml"
ENV_FILE = PROJECT_ROOT / ".env"
DEFAULT_LOG_RETENTION_DAYS = 10

# Create logs directory if it doesn't exist
LOGS_DIR = PROJECT_ROOT / "logs"
LOGS_DIR.mkdir(exist_ok=True)


def _read_env_file() -> dict[str, str]:
    """Read simple KEY=value pairs from .env without requiring python-dotenv.

    Returns:
        A mapping of environment keys to their string values. Empty if the file
        is missing or cannot be read.
    """
    if not ENV_FILE.exists():
        return {}

    values: dict[str, str] = {}
    try:
        with open(ENV_FILE, encoding="utf-8") as env_file:
            for raw_line in env_file:
                line = raw_line.strip()
                if not line or line.startswith("#"):
                    continue
                if line.startswith("export "):
                    line = line[len("export ") :].strip()
                if "=" not in line:
                    continue

                key, value = line.split("=", 1)
                key = key.strip()
                value = value.strip()

                if len(value) >= 2 and value[0] == value[-1] and value[0] in ('"', "'"):
                    value = value[1:-1]
                else:
                    value = value.split(" #", 1)[0].strip()

                values[key] = value
    except OSError:
        return {}

    return values


def _read_yaml_config() -> dict[str, Any]:
    """Read the YAML config file used for logging settings.

    Returns:
        The parsed configuration mapping, or an empty dict if the file is missing
        or cannot be parsed.
    """
    if not CONFIG_FILE.exists():
        return {}

    try:
        with open(CONFIG_FILE, encoding="utf-8") as config_file:
            return yaml.safe_load(config_file) or {}
    except Exception:
        return {}


def _get_nested_config_value(config: dict[str, Any], keys: list[str], default: Any = None) -> Any:
    """Look up a nested value in a config mapping by a list of keys.

    Args:
        config: The configuration mapping to traverse.
        keys: The ordered key path to follow into the mapping.
        default: Value to return if the path does not exist.

    Returns:
        The value at the given key path, or `default` if any key is missing.
    """
    current: Any = config
    for key in keys:
        if isinstance(current, dict) and key in current:
            current = current[key]
        else:
            return default
    return current


def _parse_positive_int(value: object) -> int | None:
    """Parse a value into a positive integer.

    Args:
        value: The raw value to parse (string, number, or None).

    Returns:
        The parsed positive integer, or None if the value is empty, not numeric,
        or not greater than zero.
    """
    if value in (None, ""):
        return None

    try:
        parsed = int(str(value).strip())
    except (TypeError, ValueError):
        return None

    return parsed if parsed > 0 else None


def _get_log_retention_days() -> int:
    """Resolve the log retention period from env vars and the YAML config.

    Checks environment variables first, then `.env`, then `config.yaml`, using the
    first positive integer found.

    Returns:
        The number of days to keep log files, or `DEFAULT_LOG_RETENTION_DAYS` if
        nothing valid is configured.
    """
    env_values = _read_env_file()
    config = _read_yaml_config()

    candidates = [
        os.environ.get("LOG_RETENTION_DAYS"),
        os.environ.get("LOG_FILE_RETENTION_DAYS"),
        env_values.get("LOG_RETENTION_DAYS"),
        env_values.get("LOG_FILE_RETENTION_DAYS"),
        _get_nested_config_value(config, ["logging", "retention_days"]),
        _get_nested_config_value(config, ["logging", "log_retention_days"]),
        _get_nested_config_value(config, ["log_retention_days"]),
    ]

    for candidate in candidates:
        parsed = _parse_positive_int(candidate)
        if parsed is not None:
            return parsed

    return DEFAULT_LOG_RETENTION_DAYS


class DailyLogFileHandler(logging.FileHandler):
    """File handler that writes one log file per day and prunes old files."""

    _LOG_DATE_PATTERNS = (
        (re.compile(r"^bot_(\d{8})\.log$"), "%Y%m%d"),
        (re.compile(r"^log_(\d{4}-\d{2}-\d{2})_\d{2}-\d{2}-\d{2}\.txt$"), "%Y-%m-%d"),
    )

    def __init__(self, logs_dir: Path, retention_days: int) -> None:
        """Initialize the handler and open the current day's log file.

        Args:
            logs_dir: Directory where daily log files are stored.
            retention_days: Number of days to keep log files before pruning.
        """
        self.logs_dir = Path(logs_dir)
        self.retention_days = max(1, int(retention_days))
        self.current_date = datetime.now().date()
        self._last_cleanup_date: date | None = None

        self.logs_dir.mkdir(exist_ok=True)
        super().__init__(self._log_path_for_date(self.current_date), mode="a", encoding="utf-8")
        self._cleanup_old_logs(self.current_date)

    def emit(self, record: logging.LogRecord) -> None:
        """Write a log record, rotating to a new file when the day changes.

        Args:
            record: The log record to emit.
        """
        try:
            record_date = datetime.fromtimestamp(record.created).date()
            if record_date != self.current_date:
                self._switch_to_date(record_date)
            super().emit(record)
        except Exception:
            self.handleError(record)

    def _log_path_for_date(self, target_date: date) -> Path:
        """Return the log file path for a given date.

        Args:
            target_date: The date whose log file path is requested.

        Returns:
            The path to that date's log file.
        """
        return self.logs_dir / f"bot_{target_date.strftime('%Y%m%d')}.log"

    def _switch_to_date(self, target_date: date) -> None:
        """Close the current log file and open the one for `target_date`.

        Args:
            target_date: The new date to start logging into.
        """
        if self.stream:
            self.stream.flush()
            self.stream.close()
            self.stream = None

        self.current_date = target_date
        self.baseFilename = os.fspath(self._log_path_for_date(target_date))
        self.stream = self._open()
        self._cleanup_old_logs(target_date)

    def _cleanup_old_logs(self, current_date: date) -> None:
        """Delete log files older than the retention window.

        Keeps exactly ``retention_days`` daily files: today plus the
        ``retention_days - 1`` preceding days. Runs at most once per date to avoid
        redundant filesystem scans.

        Args:
            current_date: The reference date used to compute the cutoff.
        """
        if self._last_cleanup_date == current_date:
            return

        # Inclusive of today, so subtract one to keep exactly retention_days files.
        oldest_allowed_date = current_date - timedelta(days=self.retention_days - 1)

        for log_path in self.logs_dir.iterdir():
            if not log_path.is_file():
                continue

            log_date = self._date_from_log_filename(log_path.name)
            if log_date and log_date < oldest_allowed_date:
                try:
                    log_path.unlink()
                except OSError:
                    pass

        self._last_cleanup_date = current_date

    @classmethod
    def _date_from_log_filename(cls, filename: str) -> date | None:
        """Extract the date encoded in a log file name.

        Args:
            filename: The bare log file name to inspect.

        Returns:
            The parsed date, or None if the name matches no known pattern.
        """
        for pattern, date_format in cls._LOG_DATE_PATTERNS:
            match = pattern.match(filename)
            if not match:
                continue
            try:
                return datetime.strptime(match.group(1), date_format).date()
            except ValueError:
                return None

        return None


# Configure logging
log_format = "%(asctime)s - %(name)s - %(levelname)s - %(message)s"
logging.basicConfig(
    level=logging.INFO,
    format=log_format,
    handlers=[
        DailyLogFileHandler(LOGS_DIR, _get_log_retention_days()),
        logging.StreamHandler(),  # Also log to console
    ],
)

logger = logging.getLogger(__name__)


def get_logger(name: str) -> logging.Logger:
    """Get a logger instance with the specified name.

    This function returns a logger instance that can be used throughout
    the application for consistent logging.

    Args:
        name (str): The name for the logger (typically __name__ from the calling module)

    Returns:
        logging.Logger: A configured logger instance
    """
    return logging.getLogger(name)
