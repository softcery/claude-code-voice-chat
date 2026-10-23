from __future__ import annotations

import sys


def main() -> None:
    if sys.argv[1:] == ["start"]:
        from .desktop import launch

        sys.exit(launch())
    from .run import serve

    serve()
