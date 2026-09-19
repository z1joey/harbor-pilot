#!/usr/bin/env python3
"""Validate Harbor config TOMLs the way Harbor 1.3.0's parser does, plus
cross-project port-overlap checks and optional pool warnings.

Usage:
    python3 validate_harbor_toml.py [--pool PATH] DRAFT.toml [OTHER.toml ...]

Since 1.3, configs live in ~/.harbor/projects/<name>.toml (the central
store); drafts can be any file. A file still named harbor.toml / .harbor.toml
in a project root is flagged: Harbor no longer reads project roots.

Exit codes: 0 = ok, 1 = errors/overlaps, 2 = cannot run (missing TOML lib / usage).

This script never spawns processes; live listening ports are checked
separately with `lsof -nP -iTCP -sTCP:LISTEN +c0` (see SKILL.md).
"""

from __future__ import annotations

import os
import re
import sys

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
if SCRIPT_DIR not in sys.path:
    sys.path.insert(0, SCRIPT_DIR)

from harbor_pool import (  # noqa: E402
    PORT_MAX,
    PORT_MIN,
    PoolError,
    default_pool_path,
    load_port_pool,
    port_in_pool,
)

PORT_ENV_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")
# user:pass@localhost in env often means docker-compose was copied into harbor.toml.
DOCKERISH_LOCAL_DB_RE = re.compile(
    r"^[^:]+://[^:]+:[^@]+@(?:localhost|127\.0\.0\.1)(?::\d+)?/", re.IGNORECASE
)


def load_toml(path):
    try:
        import tomllib
    except ModuleNotFoundError:
        try:
            import tomli as tomllib  # type: ignore
        except ModuleNotFoundError:
            print("ERROR: need tomllib (Python 3.11+) or tomli:")
            print("  uv run --with tomli python3 validate_harbor_toml.py ...")
            sys.exit(2)
    with open(path, "rb") as fh:
        return tomllib.load(fh)


def is_str(value):
    return isinstance(value, str)


def command_references_port(command, port, env_name):
    if f"${env_name}" in command or f"${{{env_name}}}" in command:
        return True
    return re.search(rf"(?<!\d){port}(?!\d)", command) is not None


def command_uses_port_env(command, env_name):
    return f"${env_name}" in command or f"${{{env_name}}}" in command


def check_process(path, index, entry, errors, warnings, pool_ranges):
    label = f"process[{index}]"
    name = entry.get("name")
    if not is_str(name) or not name.strip():
        errors.append(f"{path}: {label} needs a non-empty string \"name\".")
        return None
    label = f"process[{index}] (\"{name}\")"

    command = entry.get("command")
    if not is_str(command) or not command.strip():
        errors.append(f"{path}: {label} needs a non-empty string \"command\".")

    port = entry.get("port")
    has_port = False
    if port is not None:
        if isinstance(port, int) and not isinstance(port, bool):
            if not PORT_MIN <= port <= PORT_MAX:
                errors.append(
                    f"{path}: {label} port must be an integer in {PORT_MIN}-{PORT_MAX}."
                )
            else:
                has_port = True
        elif is_str(port) and port == "auto":
            errors.append(
                f"{path}: {label} port = \"auto\" is not allowed; "
                "register a sticky pool port (port = N) — see SKILL.md."
            )
            port = None
        elif is_str(port):
            errors.append(
                f"{path}: {label} port must be an integer in {PORT_MIN}-{PORT_MAX}, not {port!r}."
            )
            port = None
        else:
            errors.append(
                f"{path}: {label} port must be an integer in {PORT_MIN}-{PORT_MAX}."
            )
            port = None

    port_env = entry.get("port_env")
    env_name = "PORT"
    if port_env is not None:
        if not has_port:
            errors.append(
                f"{path}: {label} port_env is only allowed when port is a declared integer."
            )
        elif not is_str(port_env) or not port_env.strip():
            errors.append(f"{path}: {label} port_env must be a non-empty string.")
        elif not PORT_ENV_RE.match(port_env):
            errors.append(
                f"{path}: {label} port_env {port_env!r} is not a valid environment variable name."
            )
        else:
            env_name = port_env
    elif "port_env" in entry:
        errors.append(f"{path}: {label} port_env must be a string.")

    ready_url = entry.get("ready_url")
    if ready_url is not None:
        if not is_str(ready_url):
            errors.append(f"{path}: {label} ready_url must be a string.")
        else:
            if "${port}" in ready_url and not has_port:
                errors.append(
                    f"{path}: {label} ready_url uses ${{port}} but no integer port is declared."
                )
            validate_url = ready_url.replace("${port}", "1")
            if not re.match(r"^[a-zA-Z][a-zA-Z0-9+.-]*:", validate_url):
                errors.append(
                    f"{path}: {label} ready_url {ready_url!r} must be a URL with a scheme."
                )

    if has_port and is_str(command):
        if not command_references_port(command, port, env_name):
            warnings.append(
                f"{path}: {label} port {port} is not referenced in command "
                f"(${env_name} or {port})."
            )
        if pool_ranges is not None and command_uses_port_env(command, env_name) and not port_in_pool(port, pool_ranges):
            warnings.append(
                f"{path}: {label} port {port} is outside the Harbor pool; "
                "managed servers that take $PORT should register the next free pool port."
            )

    if "auto_restart" in entry and not isinstance(entry["auto_restart"], bool):
        errors.append(f"{path}: {label} auto_restart must be a boolean.")

    env = entry.get("env")
    if env is not None:
        if not isinstance(env, dict):
            errors.append(f"{path}: {label} env must be a table.")
        else:
            for key, value in env.items():
                if not isinstance(value, (str, int, bool)):
                    errors.append(
                        f"{path}: {label} env {key!r} must be a string, int, or boolean."
                    )

    return name, port if has_port else None


def backend_default_database_url(project_root):
    """Best-effort read of Settings.database_url default in backend/app/core/config.py."""
    config_py = os.path.join(project_root, "backend", "app", "core", "config.py")
    if not os.path.isfile(config_py):
        return None
    try:
        with open(config_py, encoding="utf-8") as fh:
            text = fh.read()
    except OSError:
        return None
    match = re.search(r'database_url:\s*str\s*=\s*"([^"]+)"', text)
    return match.group(1) if match else None


def has_docker_compose_process(data):
    processes = data.get("process", [])
    if not isinstance(processes, list):
        return False
    for entry in processes:
        if not isinstance(entry, dict):
            continue
        command = entry.get("command")
        if is_str(command) and "docker compose" in command:
            return True
    return False


def check_env_drift(path, data, warnings):
    """Warn when harbor.toml env copies Docker credentials but the app defaults to local dev."""
    project_root = os.path.dirname(os.path.abspath(path))
    processes = data.get("process", [])
    if not isinstance(processes, list):
        return
    docker_db = has_docker_compose_process(data)
    default_url = backend_default_database_url(project_root)
    for index, entry in enumerate(processes):
        if not isinstance(entry, dict):
            continue
        env = entry.get("env")
        if not isinstance(env, dict):
            continue
        proc_name = entry.get("name") if is_str(entry.get("name")) else f"process[{index}]"
        for key in ("STEWARDS_DATABASE_URL", "DATABASE_URL"):
            if key not in env:
                continue
            url = str(env[key])
            if DOCKERISH_LOCAL_DB_RE.match(url) and not docker_db:
                hint = (
                    f"{path}: {proc_name} env {key} looks Docker-specific ({url!r}). "
                    "Harbor runs on the host — use the same URL as scripts/dev or "
                    "backend/app/core/config.py (often peer auth, no user:pass on localhost), "
                    "or add a `docker compose up …` process and match compose credentials."
                )
                if default_url and url != default_url:
                    hint += f" App default: {default_url!r}."
                warnings.append(hint)
            elif default_url and url != default_url and not docker_db:
                warnings.append(
                    f"{path}: {proc_name} env {key} ({url!r}) differs from "
                    f"backend default {default_url!r} — confirm this is intentional for Harbor."
                )


def check_claim(path, index, entry, process_names, taken_ports, errors):
    label = f"port_claim[{index}]"
    port = entry.get("port")
    if not isinstance(port, int) or isinstance(port, bool) or not PORT_MIN <= port <= PORT_MAX:
        errors.append(f"{path}: {label} needs an integer \"port\" in {PORT_MIN}-{PORT_MAX}.")
        return None
    if port in taken_ports:
        errors.append(f"{path}: {label} port {port} is already declared in this config.")
    note = entry.get("note")
    if note is not None and not is_str(note):
        errors.append(f"{path}: {label} note must be a string.")
    owner = entry.get("process")
    if owner is not None and (not is_str(owner) or owner not in process_names):
        errors.append(f"{path}: {label} process {owner!r} is not defined in this config.")
    return port


def check_file(path, pool_ranges):
    """Returns (project_name, claimed_ports:set, errors:list, warnings:list)."""
    errors = []
    warnings = []
    try:
        data = load_toml(path)
    except Exception as exc:  # tomllib errors carry line/col info
        return None, set(), [f"{path}: TOML parse error: {exc}"], warnings

    if not isinstance(data, dict):
        return None, set(), [f"{path}: top level must be a table."], warnings

    check_env_drift(path, data, warnings)

    processes = data.get("process", [])
    if not isinstance(processes, list):
        return data.get("name"), set(), [f"{path}: \"process\" must be a list of tables ([[process]])."], warnings

    names, taken_ports = set(), set()
    for index, entry in enumerate(processes):
        if not isinstance(entry, dict):
            errors.append(f"{path}: process[{index}] is not a table.")
            continue
        parsed = check_process(path, index, entry, errors, warnings, pool_ranges)
        if parsed is None:
            continue
        name, port = parsed
        if name in names:
            errors.append(f"{path}: duplicate process name \"{name}\".")
        names.add(name)
        if port is not None:
            if port in taken_ports:
                errors.append(f"{path}: port {port} is declared by two processes.")
            taken_ports.add(port)

    claims = data.get("port_claim", [])
    if not isinstance(claims, list):
        errors.append(f"{path}: \"port_claim\" must be a list of tables ([[port_claim]]).")
        claims = []
    for index, entry in enumerate(claims):
        if not isinstance(entry, dict):
            errors.append(f"{path}: port_claim[{index}] is not a table.")
            continue
        port = check_claim(path, index, entry, names, taken_ports, errors)
        if port is not None:
            taken_ports.add(port)

    has_open_process = data.get("open_process") is not None
    has_open_url = data.get("open_url") is not None
    if has_open_process and has_open_url:
        errors.append(f"{path}: use either open_process or open_url, not both.")
    if has_open_process:
        open_proc = data.get("open_process")
        if not is_str(open_proc) or not open_proc.strip():
            errors.append(f"{path}: open_process must be a non-empty string.")
        elif open_proc not in names:
            errors.append(f"{path}: open_process {open_proc!r} is not defined in this config.")
    elif data.get("open_process") is not None:
        errors.append(f"{path}: open_process must be a string.")
    if has_open_url:
        open_url = data.get("open_url")
        if not is_str(open_url) or not open_url.strip():
            errors.append(f"{path}: open_url must be a non-empty string.")
        elif not re.match(r"^[a-zA-Z][a-zA-Z0-9+.-]*:", open_url):
            errors.append(f"{path}: open_url {open_url!r} must be a URL with a scheme.")
    elif data.get("open_url") is not None:
        errors.append(f"{path}: open_url must be a string.")

    project_name = data.get("name")
    return (project_name if is_str(project_name) and project_name else None), taken_ports, errors, warnings


def parse_cli(argv):
    pool_path = default_pool_path()  # ~/.harbor, with legacy App Support fallback
    paths = []
    args = argv[1:]
    index = 0
    while index < len(args):
        arg = args[index]
        if arg in ("-h", "--help"):
            print(__doc__)
            sys.exit(0)
        if arg == "--pool":
            index += 1
            if index >= len(args):
                print("ERROR    --pool requires a path", file=sys.stderr)
                sys.exit(2)
            pool_path = args[index]
        elif arg.startswith("--pool="):
            pool_path = arg.split("=", 1)[1]
        else:
            paths.append(arg)
        index += 1
    return pool_path, paths


def load_pool_for_lint(pool_path, warnings):
    try:
        return load_port_pool(pool_path, missing_ok=True)
    except PoolError as exc:
        warnings.append(f"could not load port pool ({pool_path}): {exc}")
        return None


def main():
    pool_path, paths = parse_cli(sys.argv)
    if not paths:
        print(__doc__)
        sys.exit(2)

    pool_warnings = []
    pool_ranges = load_pool_for_lint(pool_path, pool_warnings)

    results = [check_file(path, pool_ranges) for path in paths]
    errors = [error for _, _, errs, _ in results for error in errs]
    warnings = list(pool_warnings)
    warnings.extend(warn for _, _, _, warns in results for warn in warns)

    for path in paths:
        if os.path.basename(path) in ("harbor.toml", ".harbor.toml"):
            warnings.append(
                f"{path}: project-root configs are no longer read by Harbor 1.3+ — "
                "register this config in ~/.harbor/projects/ instead (the root file "
                "would be safe to git rm)")

    # Cross-project static overlaps: same fixed port claimed by two configs.
    by_port = {}
    for (name, ports, _, _), path in zip(results, paths):
        label = name or path
        for port in ports:
            by_port.setdefault(port, []).append(label)
    overlaps = {port: holders for port, holders in by_port.items() if len(holders) > 1}
    for port, holders in sorted(overlaps.items()):
        print(f"OVERLAP  port {port} claimed by: {', '.join(holders)}")

    for warning in warnings:
        print(f"WARN     {warning}")

    for error in errors:
        print(f"ERROR    {error}")

    if not errors and not overlaps:
        print("OK       all checks passed"
              + (f" ({len(results)} config(s))" if len(results) > 1 else ""))
    sys.exit(1 if errors or overlaps else 0)


if __name__ == "__main__":
    main()
