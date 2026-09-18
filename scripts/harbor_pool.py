"""Harbor 1.2.0 port-pool helpers shared by next_pool_port.py, the validator,
and register_project.py.

Harbor's on-disk state lives in the hidden `~/.harbor` folder:

    ~/.harbor/projects.json    # JSON array of registered project roots
    ~/.harbor/port-pool.json   # { "ranges": [{ "from": 8100, "to": 8199 }] }

Missing pool file → default 8100–8199. Present file must have one or more
inclusive ranges in 1…65535; inverted or overlapping ranges are rejected.

The skill can register projects while no Harbor app is running: the app only
reads (and watches) the same files. Pre-1.2 stores under
"~/Library/Application Support/Harbor" are migrated on first registration and
still honored by reads until then.
"""

from __future__ import annotations

import json
import os
import re

PORT_MIN, PORT_MAX = 1, 65535
DEFAULT_RANGES = ((8100, 8199),)
HARBOR_DIR = os.path.expanduser("~/.harbor")
DEFAULT_POOL_PATH = os.path.join(HARBOR_DIR, "port-pool.json")
DEFAULT_PROJECTS_JSON = os.path.join(HARBOR_DIR, "projects.json")

LEGACY_DIR = os.path.expanduser("~/Library/Application Support/Harbor")
LEGACY_POOL_PATH = os.path.join(LEGACY_DIR, "port-pool.json")
LEGACY_PROJECTS_JSON = os.path.join(LEGACY_DIR, "projects.json")

CONFIG_NAMES = ("harbor.toml", ".harbor.toml")
LSOF_LISTEN_RE = re.compile(r":(\d+)\s+\(LISTEN\)")


class PoolError(Exception):
    """Invalid port-pool.json / projects.json, or exhausted pool."""


def _validated_abspath(path, label):
    """Expanduser + reject relative paths and '..' segments (no traversal)."""
    expanded = os.path.expanduser(path)
    if not os.path.isabs(expanded):
        raise PoolError(f"{label} must be an absolute path: {path}")
    if ".." in expanded.split(os.sep):
        raise PoolError(f"{label} must not contain '..' segments: {path}")
    return os.path.normpath(expanded)


def default_pool_path():
    """~/.harbor/port-pool.json, or the legacy App Support file while only that exists."""
    if not os.path.isfile(DEFAULT_POOL_PATH) and os.path.isfile(LEGACY_POOL_PATH):
        return LEGACY_POOL_PATH
    return DEFAULT_POOL_PATH


def default_projects_path():
    """~/.harbor/projects.json, or the legacy App Support file while only that exists."""
    if not os.path.isfile(DEFAULT_PROJECTS_JSON) and os.path.isfile(LEGACY_PROJECTS_JSON):
        return LEGACY_PROJECTS_JSON
    return DEFAULT_PROJECTS_JSON


def migrate_legacy_stores(harbor_dir=HARBOR_DIR, legacy_dir=LEGACY_DIR):
    """One-time move of the pre-1.2 App Support stores into ~/.harbor.

    Files already in ~/.harbor win; a Harbor app racing us to the same move
    makes one side a silent no-op. Never raises for valid directories.
    """
    harbor_dir = _validated_abspath(harbor_dir, "harbor directory")
    legacy_dir = _validated_abspath(legacy_dir, "legacy directory")
    try:
        os.makedirs(harbor_dir, exist_ok=True)
        if not os.path.isdir(legacy_dir):
            return
        for name in ("projects.json", "port-pool.json"):
            src = os.path.join(legacy_dir, name)
            dst = os.path.join(harbor_dir, name)
            if os.path.isfile(src) and not os.path.exists(dst):
                os.replace(src, dst)
        for name in ("projects.json.lock", "port-pool.json.lock"):
            try:
                os.remove(os.path.join(legacy_dir, name))
            except OSError:
                pass
        try:
            os.rmdir(legacy_dir)  # succeeds only when the directory is empty
        except OSError:
            pass
    except OSError:
        pass


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
    pool_path = default_pool_path() if path is None else path
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
    registry = default_projects_path() if path is None else path
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


def register_project(root, projects_path=None):
    """Append a project root to ~/.harbor/projects.json — the file Harbor reads.

    Register only after the config exists (both flavors are detected). The
    write is idempotent and atomic, so a running Harbor app picks it up via
    its directory watcher. Returns "registered" or "already-registered".
    """
    root = _validated_abspath(root, "project root")
    if not os.path.isdir(root):
        raise PoolError(f"not a directory: {root}")
    if locate_config(root) is None:
        raise PoolError(
            f"no harbor.toml or .harbor.toml in {root} — write the config before registering"
        )
    if projects_path is None:
        migrate_legacy_stores()
        projects_path = DEFAULT_PROJECTS_JSON
    else:
        projects_path = _validated_abspath(projects_path, "projects.json path")
    try:
        with open(projects_path, encoding="utf-8") as fh:
            data = json.load(fh)
    except FileNotFoundError:
        data = []
    except (OSError, json.JSONDecodeError) as exc:
        raise PoolError(f"could not read {projects_path}: {exc}") from exc
    if not isinstance(data, list):
        raise PoolError(f"{projects_path} must be a JSON array of project root paths.")
    data = [
        os.path.normpath(os.path.expanduser(entry)) if isinstance(entry, str) else entry
        for entry in data
    ]
    if root in data:
        return "already-registered"
    data.append(root)

    import tempfile
    directory = os.path.dirname(projects_path)
    if directory:
        os.makedirs(directory, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=directory, prefix=".projects-", suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            json.dump(data, fh, indent=2)
            fh.write("\n")
        os.replace(tmp, projects_path)
    except BaseException:
        try:
            os.remove(tmp)
        except OSError:
            pass
        raise
    return "registered"
