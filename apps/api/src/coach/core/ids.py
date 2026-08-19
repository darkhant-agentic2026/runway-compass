"""Document id generation.

ULIDs: lexicographically sortable by creation time, so a raw id listing is already in
insertion order, and collision-free without coordination. The short prefixes match the
ids used throughout docs/04-api-contract.md (`t_01J…`, `r_01J…`).
"""

from __future__ import annotations

import secrets

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


def upload_id() -> str:
    return _new("up")


def trace_id() -> str:
    """Correlates a 500 the user can see with the log line that explains it.

    Only used off Cloud Run, where the platform supplies its own trace id
    (`coach.main._trace_id`). Time-sortable, which is exactly what is wanted when the
    only other thing known about an incident is roughly when it happened.
    """
    return _new("tr")


def ticket_id() -> str:
    """A single-use, 60-second WebSocket ticket.

    `secrets`, not a ULID: this is the one id in the system that is a **bearer
    credential** (docs/04-api-contract.md#authentication), and a ULID's leading 48 bits
    are a timestamp. Being one-shot and short-lived is what makes it safe to put in a
    query string; being unguessable is what makes it a credential at all, and that should
    not rest on the random half of an id designed for sorting.
    """
    return f"wst_{secrets.token_urlsafe(32)}"


def item_id() -> str:
    """Stable per-item id, on a research report item and on the task item it promotes to.

    Assigned server-side, never by the model. The report and the task item it becomes share
    one id, so a thumbs-down recorded against the recommendation and a tick recorded against
    the checklist entry are talking about the same thing. Without stable ids, both break as
    soon as a report is re-run and item order shifts.
    docs/02-data-model.md#task-items
    """
    return _new("i")
