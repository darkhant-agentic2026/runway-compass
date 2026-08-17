"""Deferring construction of a client that resolves credentials.

Every Google client in this project — Firestore, Cloud Storage, and ADK's
`GcsArtifactService` on top of it — calls `google.auth.default()` from its constructor. So
*building* one is a credentials check, and anything that builds one while assembling the
application makes the application impossible to construct without credentials.

That is not a hypothetical: `coach.main` creates the app at module scope, because
`uvicorn coach.main:app` needs it to exist. An eager client turns `import coach.main` into
a credentials check, which fails in CI at collection time and in any tool that imports the
module to read its routes. It also contradicts the invariant `repositories/firestore.py`
states on `Database`: a missing credential should be a `/readyz` failure, not a startup
crash.

Eagerness buys nothing here in exchange. A constructor validates *credentials*, never the
bucket or database it was given, so nothing about a misconfiguration is caught earlier by
building the client sooner — only by using it.

`LazyProxy` forwards attribute access to an instance built on first use. Attribute access
is the whole surface these clients present (`.collection`, `.bucket`, `.save_artifact`), so
forwarding it is enough, and the factory is called at most once.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any


class LazyProxy:
    """Stands in for `factory()` until something actually uses it."""

    def __init__(self, factory: Callable[[], Any]) -> None:
        self._factory = factory
        self._instance: Any | None = None

    def _resolve(self) -> Any:
        if self._instance is None:
            self._instance = self._factory()
        return self._instance

    def __getattr__(self, name: str) -> Any:
        # `_factory` and `_instance` are set in `__init__`, so they are found by normal
        # lookup and never route through here. Private names are refused anyway, because a
        # `__getattr__` that can recurse fails unreadably.
        if name.startswith("_"):
            raise AttributeError(name)
        return getattr(self._resolve(), name)

    def __repr__(self) -> str:
        state = "unresolved" if self._instance is None else repr(self._instance)
        return f"<LazyProxy {state}>"


__all__ = ["LazyProxy"]
