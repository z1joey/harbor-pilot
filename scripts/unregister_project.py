#!/usr/bin/env python3
"""Unregister a project by removing its central config from ~/.harbor/projects/.

Usage:
    unregister_project.py ROOT [--projects-dir DIR] [--projects-json-path PATH]

Deleting the config file IS unregistering: Harbor's directory watcher drops
the project live in both frontends. Nothing on disk inside the project root
is ever touched. The pre-1.3 projects.json mirror is kept in sync.

Exit 0 on "unregistered" / "not-registered" (both printed); 1 = error; 2 = usage.
"""

from __future__ import annotations

import os
import sys

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
if SCRIPT_DIR not in sys.path:
    sys.path.insert(0, SCRIPT_DIR)

from harbor_pool import (  # noqa: E402
    PoolError,
    unregister_project,
)


def main(argv=None):
    args = sys.argv[1:] if argv is None else argv
    roots = []
    projects_dir = None
    projects_json_path = None
    index = 0
    while index < len(args):
        arg = args[index]
        if arg in ("-h", "--help"):
            print(__doc__)
            return 0
        if arg in ("--projects-dir", "--projects-json-path"):
            index += 1
            if index >= len(args):
                print(f"ERROR    {arg} requires a path", file=sys.stderr)
                return 2
            if arg == "--projects-dir":
                projects_dir = args[index]
            else:
                projects_json_path = args[index]
        elif arg.startswith("--projects-dir="):
            projects_dir = arg.split("=", 1)[1]
        elif arg.startswith("--projects-json-path="):
            projects_json_path = arg.split("=", 1)[1]
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
        result = unregister_project(
            roots[0],
            projects_dir=projects_dir,
            projects_path=projects_json_path,
        )
    except PoolError as exc:
        print(f"ERROR    {exc}", file=sys.stderr)
        return 1
    print(f"{result.upper():<14} {os.path.normpath(os.path.expanduser(roots[0]))}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
