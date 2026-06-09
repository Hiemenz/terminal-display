"""Canonical config loader. Every module imports this."""
import logging
import os
import shutil
import yaml

logger = logging.getLogger(__name__)

_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_DEFAULT_CONFIG = os.path.join(_REPO_ROOT, 'config', 'config.yaml')
_ENV_FILE = os.path.join(_REPO_ROOT, '.env')


def _load_dotenv():
    """Load key=value pairs from .env into os.environ (existing vars are not overwritten)."""
    if not os.path.exists(_ENV_FILE):
        return
    with open(_ENV_FILE) as f:
        for line in f:
            line = line.strip()
            if not line or line.startswith('#') or '=' not in line:
                continue
            key, _, value = line.partition('=')
            key = key.strip()
            value = value.strip().strip('"').strip("'")
            if key and key not in os.environ:
                os.environ[key] = value


_load_dotenv()


def load_config(config_path=None):
    """Load config.yaml, returning a dict.

    On success the file is snapshotted to ``<path>.bak`` (last-known-good). If
    the config is corrupt (e.g. a half-written save), we fall back to that
    backup instead of silently returning an empty config that drops every
    setting. Both failures are logged so they're visible in journald.
    """
    path = config_path or _DEFAULT_CONFIG
    try:
        with open(path, 'r') as f:
            data = yaml.safe_load(f)
        if not isinstance(data, dict):
            raise ValueError(f"config did not parse to a mapping (got {type(data).__name__})")
        # Snapshot last-known-good for the fallback path below.
        try:
            shutil.copyfile(path, path + '.bak')
        except OSError:
            pass
        return data
    except FileNotFoundError:
        logger.warning("Config file not found: %s — using defaults", path)
        return {}
    except Exception as e:
        logger.error("Error loading config %s: %s", path, e)
        backup = path + '.bak'
        if os.path.exists(backup):
            try:
                with open(backup) as f:
                    data = yaml.safe_load(f)
                if isinstance(data, dict):
                    logger.warning("Recovered config from last-known-good backup %s", backup)
                    return data
            except Exception as be:
                logger.error("Backup config also unreadable %s: %s", backup, be)
        return {}


def add_config_arg(parser):
    """Add --config PATH argument to any ArgumentParser."""
    parser.add_argument(
        '--config',
        type=str,
        default=None,
        metavar='PATH',
        help=f'Path to config.yaml (default: {_DEFAULT_CONFIG})',
    )
