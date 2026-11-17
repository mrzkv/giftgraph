"""Load CLI configuration without executing files or retaining credentials."""

import os
from contextlib import contextmanager
from pathlib import Path

from dotenv.parser import parse_stream

ENV_OPTIONS = {
    "session": "GIFTGRAPH_SESSION",
    "db": "GIFTGRAPH_DB",
    "output_dir": "GIFTGRAPH_OUTPUT_DIR",
    "depth": "GIFTGRAPH_DEPTH",
    "seed_limit": "GIFTGRAPH_SEED_LIMIT",
    "max_nodes": "GIFTGRAPH_MAX_NODES",
    "max_gifts": "GIFTGRAPH_MAX_GIFTS",
    "max_pages": "GIFTGRAPH_MAX_PAGES",
    "page_size": "GIFTGRAPH_PAGE_SIZE",
    "delay": "GIFTGRAPH_DELAY",
    "gift_description": "GIFTGRAPH_GIFT_DESCRIPTION",
}
ENV_KEYS = frozenset(ENV_OPTIONS.values()) | {"API_ID", "API_HASH"}


@contextmanager
def environment_file(path: Path | None = None):
    """Temporarily fill missing settings; the shell environment always wins.

    Use the dotenv parser directly to reject malformed lines and keep values
    literal (no interpolation). Only recognized application settings are loaded.
    """
    selected = path.expanduser() if path is not None else Path(".env")
    values: dict[str, str] = {}
    try:
        with selected.open(encoding="utf-8-sig") as stream:
            for binding in parse_stream(stream):
                if binding.error:
                    raise ValueError(
                        f"Некорректный .env: {selected}, строка {binding.original.line}"
                    )
                if binding.key in ENV_KEYS and binding.value is not None:
                    values[binding.key] = binding.value
    except FileNotFoundError:
        if path is not None:
            raise ValueError(f"Файл конфигурации не найден: {selected}") from None
    except (OSError, UnicodeError):
        raise ValueError(f"Не удалось прочитать файл конфигурации: {selected}") from None

    added = {
        key: value for key, value in values.items() if value.strip() and key not in os.environ
    }
    try:
        os.environ.update(added)
        yield
    finally:
        for key in added:
            os.environ.pop(key, None)
