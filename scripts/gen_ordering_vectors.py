#!/usr/bin/env python
"""Generate the cross-language fractional-index parity vectors.

`apps/api/src/coach/services/ordering.py` and `apps/web/src/lib/ordering.ts` implement the
same algorithm in two languages, and the board's optimistic drag-and-drop is only correct
while they agree exactly (docs/06-frontend.md, docs/08-testing.md). This script runs the
**Python** implementation and writes its answers to
`apps/web/src/lib/ordering-vectors.json`, which `ordering.test.ts` replays against the
TypeScript port.

Run it via `./scripts/dev.sh gen-ordering-vectors` after touching either implementation.
The output is committed, so a drift shows up as a failing test rather than as a file that
silently regenerates.
"""

from __future__ import annotations

import json
import random
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT / "apps" / "api" / "src"))

from coach.services.ordering import key_between, n_keys_between, rebalance  # noqa: E402

OUTPUT = REPO_ROOT / "apps" / "web" / "src" / "lib" / "ordering-vectors.json"

#: Fixed so the vectors are reproducible; regenerating without a change must be a no-op.
SEED = 20260814


def main() -> int:
    random.seed(SEED)
    vectors: dict[str, list[dict[str, object]]] = {
        "keyBetween": [],
        "nKeysBetween": [],
        "rebalance": [],
        "sequence": [],
    }

    # Boundary pairs, then a walk far enough up the key space to cross an integer-part
    # length change — which is where the two implementations are most likely to disagree.
    pairs: list[tuple[str | None, str | None]] = [
        (None, None),
        ("a0", None),
        (None, "a0"),
        ("a0", "a1"),
        ("a0", "a0V"),
        ("Zz", "a0"),
    ]
    keys = ["a0"]
    for _ in range(60):
        keys.append(key_between(keys[-1], None))
    for index in range(0, 50, 7):
        pairs.append((keys[index], keys[index + 1]))
    for a, b in pairs:
        vectors["keyBetween"].append({"a": a, "b": b, "expected": key_between(a, b)})

    for n in (0, 1, 2, 5, 17):
        vectors["nKeysBetween"].append(
            {"a": "a0", "b": "a1", "n": n, "expected": n_keys_between("a0", "a1", n)}
        )
        vectors["nKeysBetween"].append(
            {"a": None, "b": None, "n": n, "expected": n_keys_between(None, None, n)}
        )

    for n in (0, 1, 3, 12):
        vectors["rebalance"].append({"n": n, "expected": rebalance(n)})

    board: list[str] = []
    for _ in range(30):
        at = random.randint(0, len(board))
        previous = board[at - 1] if at > 0 else None
        following = board[at] if at < len(board) else None
        key = key_between(previous, following)
        board.insert(at, key)
        vectors["sequence"].append({"at": at, "key": key, "board": list(board)})

    OUTPUT.write_text(json.dumps(vectors, indent=1) + "\n", encoding="utf-8")
    print(f"wrote {OUTPUT.relative_to(REPO_ROOT)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
