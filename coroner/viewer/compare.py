"""`/compare` query validation.

Pure string handling, no file access. The page resolves the slots itself through
the API; this module only decides whether the query string is acceptable.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass

from coroner.runner.manifest import RunIdError, validate_run_id_for_path

__all__ = ["CompareQuery", "QueryError", "parse_compare_query"]

_SLOTS = ("a", "b", "c")

CODE_INVALID_QUERY = "invalid_query"
CODE_INVALID_RUN_ID = "invalid_run_id"


class QueryError(ValueError):
    """The compare query is not acceptable (HTTP 400). Never carries a request value."""

    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


@dataclass(frozen=True)
class CompareQuery:
    """Validated slot ids. ``None`` means the slot was not requested."""

    a: str | None
    b: str | None
    c: str | None


def parse_compare_query(items: Sequence[tuple[str, str]]) -> CompareQuery:
    """Validate ``request.query_params.multi_items()``.

    Only ``a``, ``b`` and ``c`` are accepted, each at most once, never empty, and
    each value must pass ``validate_run_id_for_path``. Every item is checked
    before anything is returned, and nothing here touches the file system.
    """
    seen: dict[str, str] = {}
    for key, value in items:
        if key not in _SLOTS or key in seen or value == "":
            raise QueryError(CODE_INVALID_QUERY)
        seen[key] = value
    for value in seen.values():
        try:
            validate_run_id_for_path(value)
        except RunIdError:
            raise QueryError(CODE_INVALID_RUN_ID) from None
    return CompareQuery(a=seen.get("a"), b=seen.get("b"), c=seen.get("c"))
