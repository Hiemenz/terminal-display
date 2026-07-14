"""Shared filesystem helpers."""
import json
import os

import yaml

_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_DATA_DIR = os.path.join(_REPO_ROOT, 'data')
_CONFIG_DIR = os.path.join(_REPO_ROOT, 'config')
_OUTPUT_DIR = os.path.join(_REPO_ROOT, 'output')


def data_path(filename):
    return os.path.join(_DATA_DIR, filename)


def output_path(filename):
    return os.path.join(_OUTPUT_DIR, filename)


def load_json_file(file_name, file_path=None):
    base = file_path if file_path is not None else _DATA_DIR
    full = os.path.join(base, file_name)
    try:
        if not os.path.isfile(full):
            return {}
        with open(full, 'r') as f:
            return json.load(f)
    except Exception:
        return {}


def save_json_file(data, file_name, file_path=None):
    base = file_path if file_path is not None else _DATA_DIR
    os.makedirs(base, exist_ok=True)
    full = os.path.join(base, file_name)
    with open(full, 'w') as f:
        json.dump(data, f, indent=2)


def load_yaml_file(file_name, file_path=None):
    base = file_path if file_path is not None else _CONFIG_DIR
    full = os.path.join(base, file_name)
    try:
        if not os.path.isfile(full):
            return {}
        with open(full, 'r') as f:
            data = yaml.safe_load(f)
            return data if data else {}
    except Exception as e:
        print(f'Error parsing YAML: {e}')
        return {}
