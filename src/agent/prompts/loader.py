"""Loads prompts.json and fills <<placeholders>>. All prompt text lives in prompts.json."""
import json
import re
from functools import lru_cache
from pathlib import Path

_PATH = Path(__file__).with_name("prompts.json")
_PLACEHOLDER = re.compile(r"<<(\w+)>>")


@lru_cache(maxsize=1)
def load() -> dict:
    return json.loads(_PATH.read_text(encoding="utf-8"))


def get(name: str) -> dict:
    return load()["prompts"][name]


def enums() -> dict:
    return load()["enums"]


def fill(text: str, **values) -> str:
    """Replaces <<name>> with values[name]. A missing value raises, so nothing silently drops."""
    def sub(m: re.Match) -> str:
        key = m.group(1)
        if key not in values:
            raise KeyError(f"missing prompt value: {key}")
        return str(values[key])

    return _PLACEHOLDER.sub(sub, text)
