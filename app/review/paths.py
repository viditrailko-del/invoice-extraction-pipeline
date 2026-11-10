"""Read/write a value inside invoice JSON by a field path like "line_items[2].amount".

The same path format is used in field_flags.field_path and corrections.field_path,
so a reviewer can take a flag's path and send a correction for exactly that field.
Pure functions, no DB.
"""

import re
from typing import Any

PathPart = str | int

_TOKEN = re.compile(r"([A-Za-z_][A-Za-z0-9_]*)((?:\[\d+\])*)")


class InvalidPath(ValueError):
    pass


def parse_path(path: str) -> list[PathPart]:
    """'line_items[2].amount' -> ['line_items', 2, 'amount']."""
    parts: list[PathPart] = []
    for segment in path.split("."):
        m = _TOKEN.fullmatch(segment)
        if not m:
            raise InvalidPath(f"Malformed field path {path!r}")
        parts.append(m.group(1))
        parts.extend(int(i) for i in re.findall(r"\[(\d+)\]", m.group(2)))
    return parts


def _step(container: Any, part: PathPart, path: str) -> Any:
    if isinstance(part, int):
        if not isinstance(container, list) or part >= len(container):
            raise InvalidPath(f"No such field {path!r} (index {part} out of range)")
        return container[part]
    if not isinstance(container, dict) or part not in container:
        raise InvalidPath(f"No such field {path!r}")
    return container[part]


def get_at(data: Any, path: str) -> Any:
    node = data
    for part in parse_path(path):
        node = _step(node, part, path)
    return node


def set_at(data: Any, path: str, value: Any) -> None:
    """Replace an EXISTING field. Never creates keys or list slots: to add or remove
    line items, replace the whole "line_items" list."""
    parts = parse_path(path)
    parent = data
    for part in parts[:-1]:
        parent = _step(parent, part, path)
    _step(parent, parts[-1], path)  # must already exist
    parent[parts[-1]] = value
