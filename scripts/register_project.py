#!/usr/bin/env python3
"""Register a project in Harbor by appending it to ~/.harbor/projects.json.

Usage:
    register_project.py ROOT [--projects-json-path PATH]

Harbor's app and TUI only read (and watch) that file, so this works whether or
not any Harbor frontend is running — a running app shows the project within a
second. The folder must already contain harbor.toml / .harbor.toml; the write
is idempotent and atomic.

Exit 0 on "registered" / "already-registered" (both printed); 1 = error; 2 = usage.
"""

from __future__ import annotations

import os
import sys

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
if SCRIPT_DIR not in sys.path:
    sys.path.insert(0, SCRIPT_DIR)

from harbor_pool import (  # noqa: E402
    DEFAULT_PROJECTS_JSON,
    PoolError,
    register_project,
)


def main(argv=None):
    args = sys.argv[1:] if argv is None else argv
    roots = []
    projects_path = None
    index = 0
    while index < len(args):
        arg = args[index]
        if arg in ("-h", "--help"):
            print(__doc__)
            return 0
        if arg == "--projects-json-path":
            index += 1
            if index >= len(args):
                print("ERROR    --projects-json-path requires a path", file=sys.stderr)
                return 2
            projects_path = args[index]
        elif arg.startswith("--projects-json-path="):
            projects_path = arg.split("=", 1)[1]
        elif arg.startswith("-"):
            print(f"ERROR    unknown option {arg}", file=sys.stderr)
            return 2
        else:
            roots.append(arg)
        index += 1
    if len(roots) != 1:
        print(__doc__)
        return 2
    try:
        result = register_project(roots[0], projects_path=projects_path)
    except PoolError as exc:
        print(f"ERROR    {exc}", file=sys.stderr)
        return 1
    print(f"{result.upper():<8} {os.path.normpath(os.path.expanduser(roots[0]))}"
          + ("" if projects_path else f"  ({DEFAULT_PROJECTS_JSON})"))
    return 0


if __name__ == "__main__":
    sys.exit(main())
