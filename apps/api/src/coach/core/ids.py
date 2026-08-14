"""Document id generation.

ULIDs: lexicographically sortable by creation time, so a raw id listing is already in
insertion order, and collision-free without coordination. The short prefixes match the
ids used throughout docs/04-api-contract.md (`t_01J…`, `r_01J…`).
"""

from __future__ import annotations

from ulid import ULID


def _new(prefix: str) -> str:
    return f"{prefix}_{ULID()}"


def project_id() -> str:
    return _new("p")


def task_id() -> str:
    return _new("k")


def turn_id() -> str:
    return _new("t")


def run_id() -> str:
    return _new("r")


def report_id() -> str:
    return _new("rep")


def item_id() -> str:
    """Stable per-item id inside a research report.

    Assigned server-side, never by the model: without stable ids, per-item completion
    breaks as soon as a report is re-run and item order shifts.
    docs/02-data-model.md
    """
    return _new("i")
