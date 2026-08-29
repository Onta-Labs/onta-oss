"""``python -m ontology_skills`` → harness stub or local scorer."""

from __future__ import annotations

import sys


def main(argv: list[str] | None = None) -> int:
    args = list(sys.argv[1:] if argv is None else argv)
    if args and args[0] == "score":
        from .scoring import score_main

        return score_main(args[1:])
    from .harness import main as harness_main

    return harness_main(args)


if __name__ == "__main__":
    raise SystemExit(main())
