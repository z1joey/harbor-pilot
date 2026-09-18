#!/usr/bin/env python3
"""Print the next free Harbor pool port.

Usage:
    python3 next_pool_port.py [--pool PATH] [--count N] [--lsof]
                              [--projects-json] [--projects-json-path PATH]
                              [--stdin-taken] [TOML-or-project-dir ...]

Reads the pool from ~/.harbor/port-pool.json (falling back to the legacy
"~/Library/Application Support/Harbor/port-pool.json" while only that
exists). If neither file exists, uses 8100–8199. Skips ports claimed by the
given harbor.toml files (process port + port_claim). Directories are
resolved to harbor.toml / .harbor.toml.

    --projects-json         also load every registered project's config
                           from ~/.harbor/projects.json
    --projects-json-path P  same, but from an explicit registry path
    --lsof                 treat current TCP listeners as taken
    --stdin-taken          read extra taken ports from stdin (one int per line)
    --count N              print N free ports, one per line

Exit 0 prints ports; 1 = exhausted/invalid pool or unreadable TOML; 2 = usage.
"""

from __future__ import annotations

import argparse
import os
import sys

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
if SCRIPT_DIR not in sys.path:
    sys.path.insert(0, SCRIPT_DIR)

from harbor_pool import (  # noqa: E402
    PoolError,
    claimed_ports_from_toml,
    configs_from_projects_json,
    default_pool_path,
    default_projects_path,
    live_listen_ports,
    load_port_pool,
    next_free_ports,
    parse_taken_ports_text,
    resolve_toml_arg,
)


def build_parser():
    parser = argparse.ArgumentParser(
        description="Print the next free Harbor pool port.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )
    parser.add_argument(
        "--pool",
        metavar="PATH",
        default=None,
        help="port-pool.json path (default: ~/.harbor/port-pool.json with legacy "
             "App Support fallback; missing → 8100–8199)",
    )
    parser.add_argument(
        "--count",
        type=int,
        default=1,
        metavar="N",
        help="how many free ports to print (default: 1)",
    )
    parser.add_argument(
        "--lsof",
        action="store_true",
        help="include live TCP LISTEN ports from lsof as taken",
    )
    parser.add_argument(
        "--projects-json",
        action="store_true",
        help="also load TOMLs listed in ~/.harbor/projects.json",
    )
    parser.add_argument(
        "--projects-json-path",
        metavar="PATH",
        default=None,
        help="load TOMLs from this projects.json instead of the default registry",
    )
    parser.add_argument(
        "--stdin-taken",
        action="store_true",
        help="read extra taken ports from stdin, one integer per line",
    )
    parser.add_argument(
        "tomls",
        nargs="*",
        metavar="TOML",
        help="sibling harbor.toml paths or project directories",
    )
    return parser


def collect_taken(toml_paths, projects_json, use_lsof, stdin_taken):
    taken = set()
    configs = []
    for arg in toml_paths:
        configs.append(resolve_toml_arg(arg))
    if projects_json is not None:
        configs.extend(configs_from_projects_json(projects_json))
    seen = set()
    for path in configs:
        real = os.path.abspath(path)
        if real in seen:
            continue
        seen.add(real)
        taken |= claimed_ports_from_toml(path)
    if use_lsof:
        taken |= live_listen_ports()
    if stdin_taken:
        taken |= parse_taken_ports_text(sys.stdin.read())
    return taken


def main(argv=None):
    parser = build_parser()
    args = parser.parse_args(argv)
    if args.count < 1:
        print("ERROR    --count must be >= 1", file=sys.stderr)
        return 2
    try:
        registry = args.projects_json_path
        if registry is None and args.projects_json:
            registry = default_projects_path()
        pool = args.pool or default_pool_path()
        ranges = load_port_pool(pool, missing_ok=True)
        taken = collect_taken(args.tomls, registry, args.lsof, args.stdin_taken)
        ports = next_free_ports(ranges, taken, count=args.count)
    except PoolError as exc:
        print(f"ERROR    {exc}", file=sys.stderr)
        return 1
    except Exception as exc:  # TOML parse, JSON, etc.
        print(f"ERROR    {exc}", file=sys.stderr)
        return 1
    for port in ports:
        print(port)
    return 0


if __name__ == "__main__":
    sys.exit(main())
