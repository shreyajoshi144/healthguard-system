"""Small SQL helpers shared by the API layer."""
from __future__ import annotations


def escape_like(value: str, escape: str = "\\") -> str:
    """Escape LIKE/ILIKE wildcards so user input is matched literally. Use with ``column.ilike(pattern, escape="\\\\")``."""
    return value.replace(escape, escape * 2).replace("%", escape + "%").replace("_", escape + "_")
