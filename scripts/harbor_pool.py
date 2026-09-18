"""Harbor 1.1.0 port-pool helpers shared by next_pool_port.py and the validator.

Pool file (beside projects.json):

    ~/Library/Application Support/Harbor/port-pool.json

    { "ranges": [{ "from": 8100, "to": 8199 }] }

Missing file → default 8100–8199. Present file must have one or more inclusive
ranges in 1…65535; inverted or overlapping ranges are rejected.
"""

from __future__ import annotations

import json
import os
import re

PORT_MIN, PORT_MAX = 1, 65535
DEFAULT_RANGES = ((8100, 8199),)
DEFAULT_POOL_PATH = os.path.expanduser(
    "~/Library/Application Support/Harbor/port-pool.json"
)
DEFAULT_PROJECTS_JSON = os.path.expanduser(
    "~/Library/Application Support/Harbor/projects.json"
)
CONFIG_NAMES = ("harbor.toml", ".harbor.toml")
LSOF_LISTEN_RE = re.compile(r":(\d+)\s+\(LISTEN\)")


class PoolError(Exception):
    """Invalid port-pool.json or exhausted pool."""


def load_toml(path):
    try:
        import tomllib
    except ModuleNotFoundError:
        try:
            import tomli as tomllib  # type: ignore
        except ModuleNotFoundError as exc:
            raise PoolError(
                "need tomllib (Python 3.11+) or tomli to read harbor.toml"
            ) from exc
    with open(path, "rb") as fh:
        return tomllib.load(fh)


def _as_port_int(value):
    return isinstance(value, int) and not isinstance(value, bool) and PORT_MIN <= value <= PORT_MAX


def parse_pool_data(data):
    """Return a list of (from, to) tuples in file order."""
    if not isinstance(data, dict) or "ranges" not in data:
        raise PoolError('port-pool.json must be an object with a "ranges" array.')
    raw = data["ranges"]
    if not isinstance(raw, list) or not raw:
        raise PoolError("port-pool.json ranges must be a non-empty array.")
    parsed = []
    for index, entry in enumerate(raw):
        label = f"ranges[{index}]"
        if not isinstance(entry, dict) or "from" not in entry or "to" not in entry:
            raise PoolError(f"{label} must be an object with integer from/to.")
        start, end = entry["from"], entry["to"]
        if not isinstance(start, int) or isinstance(start, bool) or not isinstance(end, int) or isinstance(end, bool):
            raise PoolError(f"{label} from/to must be integers.")
        if not PORT_MIN <= start <= PORT_MAX or not PORT_MIN <= end <= PORT_MAX:
            raise PoolError(f"{label} from/to must be in {PORT_MIN}–{PORT_MAX}.")
        if start > end:
            raise PoolError(f"{label} is inverted: {start} > {end}.")
        parsed.append((start, end))

    ordered = sorted(enumerate(parsed), key=lambda item: (item[1][0], item[1][1], item[0]))
    for (_, (a0, a1)), (_, (b0, b1)) in zip(ordered, ordered[1:]):
        if b0 <= a1:
            raise PoolError(f"port-pool.json ranges overlap: {a0}–{a1} and {b0}–{b1}.")
    return parsed


def load_port_pool(path=None, missing_ok=True):
    """Load inclusive pool ranges. Missing file → default 8100–8199 when missing_ok."""
    pool_path = DEFAULT_POOL_PATH if path is None else path
    if not os.path.isfile(pool_path):
        if missing_ok:
            return list(DEFAULT_RANGES)
        raise PoolError(f"port pool file not found: {pool_path}")
    try:
        with open(pool_path, encoding="utf-8") as fh:
            data = json.load(fh)
    except (OSError, json.JSONDecodeError) as exc:
        raise PoolError(f"could not read {pool_path}: {exc}") from exc
    return parse_pool_data(data)


def port_in_pool(port, ranges):
    return any(start <= port <= end for start, end in ranges)


def iter_pool_ports(ranges):
    for start, end in ranges:
        yield from range(start, end + 1)


def next_free_ports(ranges, taken, count=1):
    if count < 1:
        raise PoolError("count must be >= 1")
    found = []
    claimed = set(taken)
    for port in iter_pool_ports(ranges):
        if port in claimed:
            continue
        found.append(port)
        claimed.add(port)
        if len(found) >= count:
            return found
    raise PoolError("no free port left in the configured Harbor pool")


def claimed_ports_from_data(data):
    """Process `port` + `port_claim` integers. Ignores legacy port = \"auto\"."""
    taken = set()
    if not isinstance(data, dict):
        return taken
    processes = data.get("process", [])
    if isinstance(processes, list):
        for entry in processes:
            if not isinstance(entry, dict):
                continue
            port = entry.get("port")
            if _as_port_int(port):
                taken.add(port)
    claims = data.get("port_claim", [])
    if isinstance(claims, list):
        for entry in claims:
            if not isinstance(entry, dict):
                continue
            port = entry.get("port")
            if _as_port_int(port):
                taken.add(port)
    return taken


def claimed_ports_from_toml(path):
    data = load_toml(path)
    return claimed_ports_from_data(data)


def locate_config(root):
    for name in CONFIG_NAMES:
        candidate = os.path.join(root, name)
        if os.path.isfile(candidate):
            return candidate
    return None


def resolve_toml_arg(path):
    """Accept a harbor.toml path or a project directory containing one."""
    if os.path.isdir(path):
        found = locate_config(path)
        if found is None:
            raise PoolError(f"no harbor.toml or .harbor.toml in directory {path}")
        return found
    if not os.path.isfile(path):
        raise PoolError(f"not a file or directory: {path}")
    return path


def configs_from_projects_json(path=None):
    """Return harbor.toml paths for every root listed in projects.json."""
    registry = DEFAULT_PROJECTS_JSON if path is None else path
    if not os.path.isfile(registry):
        return []
    with open(registry, encoding="utf-8") as fh:
        data = json.load(fh)
    if not isinstance(data, list):
        raise PoolError(f"{registry} must be a JSON array of project root paths.")
    configs = []
    for root in data:
        if not isinstance(root, str) or not root.strip():
            continue
        found = locate_config(os.path.expanduser(root))
        if found:
            configs.append(found)
    return configs


def parse_lsof_listen_ports(text):
    return {int(match.group(1)) for match in LSOF_LISTEN_RE.finditer(text)}


def live_listen_ports():
    import subprocess

    try:
        result = subprocess.run(
            ["lsof", "-nP", "-iTCP", "-sTCP:LISTEN", "+c0"],
            check=False,
            capture_output=True,
            text=True,
        )
    except OSError as exc:
        raise PoolError(f"could not run lsof: {exc}") from exc
    return parse_lsof_listen_ports(result.stdout or "")


def parse_taken_ports_text(text):
    """Integers from stdin / a file: one per line, optional # comments."""
    taken = set()
    for raw in text.splitlines():
        line = raw.split("#", 1)[0].strip()
        if not line:
            continue
        try:
            port = int(line)
        except ValueError as exc:
            raise PoolError(f"stdin taken-port is not an integer: {line!r}") from exc
        if not PORT_MIN <= port <= PORT_MAX:
            raise PoolError(f"stdin taken-port {port} is out of range.")
        taken.add(port)
    return taken
