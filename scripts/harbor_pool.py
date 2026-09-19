"""Harbor 1.3.0 central-store helpers shared by next_pool_port.py, the
validator, and register/unregister_project.py.

Harbor's on-disk state lives in the hidden `~/.harbor` folder and is the
single source of truth:

    ~/.harbor/projects/<name>.toml  # one config per project; the directory
                                    # listing IS the registry (v1.3+)
    ~/.harbor/projects.json         # derived mirror of roots for pre-1.3
                                    # consumers, kept in sync by this skill
    ~/.harbor/port-pool.json        # { "ranges": [{ "from": 8100, "to": 8199 }] }

Each central config carries a top-level `root = "/absolute/path"` key naming
the project folder; Harbor runs commands there and never reads the project
folder for config. Missing pool file → default 8100–8199. Present file must
have one or more inclusive ranges in 1…65535; inverted or overlapping ranges
are rejected.

The skill can register projects while no Harbor app is running: the app only
reads (and watches) the same files. Pre-1.2 state (App Support stores,
per-root harbor.toml files listed in projects.json) is migrated into the
central store on first registration and by the app at startup.
"""

from __future__ import annotations

import json
import os
import re
import sys

PORT_MIN, PORT_MAX = 1, 65535
DEFAULT_RANGES = ((8100, 8199),)
HARBOR_DIR = os.path.expanduser("~/.harbor")
DEFAULT_POOL_PATH = os.path.join(HARBOR_DIR, "port-pool.json")
DEFAULT_PROJECTS_JSON = os.path.join(HARBOR_DIR, "projects.json")
PROJECTS_DIR = os.path.join(HARBOR_DIR, "projects")

LEGACY_DIR = os.path.expanduser("~/Library/Application Support/Harbor")
LEGACY_POOL_PATH = os.path.join(LEGACY_DIR, "port-pool.json")
LEGACY_PROJECTS_JSON = os.path.join(LEGACY_DIR, "projects.json")

CONFIG_NAMES = ("harbor.toml", ".harbor.toml")
LSOF_LISTEN_RE = re.compile(r":(\d+)\s+\(LISTEN\)")


class PoolError(Exception):
    """Invalid port-pool.json / registry state, exhausted pool, or bad config."""


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


# ---------------------------------------------------------------------------
# TOML
# ---------------------------------------------------------------------------

def _toml_module():
    try:
        import tomllib
        return tomllib
    except ModuleNotFoundError:
        try:
            import tomli  # type: ignore
            return tomli
        except ModuleNotFoundError as exc:
            raise PoolError(
                "need tomllib (Python 3.11+) or tomli to read Harbor configs"
            ) from exc


def load_toml(path):
    with open(path, "rb") as fh:
        return _toml_module().load(fh)


def loads_toml(text):
    return _toml_module().loads(text)


# ---------------------------------------------------------------------------
# Central config store (~/.harbor/projects/)
# ---------------------------------------------------------------------------

def list_central_configs(projects_dir=None):
    """Sorted *.toml paths in the central store; dotfiles/temp files skipped."""
    directory = projects_dir or PROJECTS_DIR
    if not os.path.isdir(directory):
        return []
    return sorted(
        os.path.join(directory, name)
        for name in os.listdir(directory)
        if name.endswith(".toml") and not name.startswith(".")
    )


def central_config_root(path):
    """The expanded/normalized `root` of a central config; None if unusable."""
    try:
        data = load_toml(path)
    except Exception:
        return None
    if not isinstance(data, dict):
        return None
    root = data.get("root")
    if not isinstance(root, str) or not root.strip():
        return None
    return os.path.normpath(os.path.expanduser(root))


def find_central_config_for_root(root, projects_dir=None):
    """The central config declaring `root`, or None. First match by filename."""
    root = os.path.normpath(os.path.expanduser(root))
    for path in list_central_configs(projects_dir):
        if central_config_root(path) == root:
            return path
    return None


def configs_from_central_store(projects_dir=None):
    """Central config paths for every registered project (v1.3+)."""
    return list_central_configs(projects_dir)


def _slugify(name):
    slug = re.sub(r"[^A-Za-z0-9._+-]", "-", name).strip("-.")
    return slug or "project"


def _compose_config(text, root):
    """TOML text with a top-level `root = "/abs/path"` key injected/updated.

    The root key must be top-level (before the first table header); an
    existing one is replaced in place, otherwise it is prepended.
    """
    root = os.path.normpath(os.path.expanduser(root))
    lines = text.splitlines()
    root_line = f'root = "{root}"'
    index = 0
    root_at = None
    while index < len(lines):
        stripped = lines[index].strip()
        if stripped.startswith("["):
            break  # table header — a root key after this would not be top-level
        if re.match(r"root\s*=", stripped):
            root_at = index
            break
        index += 1
    if root_at is None:
        lines.insert(0, root_line)
    else:
        lines[root_at] = root_line
    return "\n".join(lines) + "\n"


def _atomic_write(path, content):
    import tempfile
    directory = os.path.dirname(path)
    os.makedirs(directory, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=directory, prefix=".harbor-", suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write(content)
        os.replace(tmp, path)
    except BaseException:
        try:
            os.remove(tmp)
        except OSError:
            pass
        raise


def install_config(text, root, projects_dir=None, replace=None):
    """Compose `text` with the root key and write it as a central config.

    `replace` names an existing central file to update in place; otherwise a
    `<slug>.toml` filename is derived from the config's `name` (falling back
    to the root folder name) with collision suffixes -2, -3, … Returns the
    written path.
    """
    directory = projects_dir or PROJECTS_DIR
    content = _compose_config(text, root)
    if replace is not None:
        _atomic_write(replace, content)
        return replace
    display = None
    try:
        data = loads_toml(content)
        if isinstance(data, dict):
            display = data.get("name")
    except Exception:
        display = None
    if not (isinstance(display, str) and display.strip()):
        display = os.path.basename(os.path.normpath(os.path.expanduser(root)))
    slug = _slugify(display)
    candidate = os.path.join(directory, f"{slug}.toml")
    suffix = 2
    while os.path.exists(candidate):
        candidate = os.path.join(directory, f"{slug}-{suffix}.toml")
        suffix += 1
    _atomic_write(candidate, content)
    return candidate


def migrate_legacy_configs(projects_dir=None, registry_path=None):
    """One-time import of per-root harbor.toml files into ~/.harbor/projects/.

    Mirrors the app's migrateLegacyConfigsIfNeeded: for every root in the
    v1.2 registry (projects.json, falling back to the legacy App Support
    copy while only that exists) with no central config already declaring
    it, the root's harbor.toml / .harbor.toml — if any — is copied into the
    central store with a `root` key injected. Existing central files win;
    the legacy registry and the root files themselves are never modified or
    deleted. Idempotent.
    """
    directory = projects_dir or PROJECTS_DIR
    try:
        os.makedirs(directory, exist_ok=True)
    except OSError:
        return

    if registry_path is not None:
        registry = registry_path
    else:
        registry = DEFAULT_PROJECTS_JSON
        if not os.path.isfile(registry) and os.path.isfile(LEGACY_PROJECTS_JSON):
            registry = LEGACY_PROJECTS_JSON
    roots = []
    if os.path.isfile(registry):
        try:
            with open(registry, encoding="utf-8") as fh:
                data = json.load(fh)
        except (OSError, json.JSONDecodeError):
            return
        if isinstance(data, list):
            roots = [entry for entry in data if isinstance(entry, str) and entry.strip()]

    covered = {
        central_config_root(path)
        for path in list_central_configs(directory)
    }
    covered.discard(None)

    for raw in roots:
        root = os.path.normpath(os.path.expanduser(raw))
        if root in covered:
            continue
        config = locate_config(root)
        if config is None:
            continue
        try:
            with open(config, encoding="utf-8") as fh:
                text = fh.read()
        except OSError:
            continue
        install_config(text, root, projects_dir=directory)


# ---------------------------------------------------------------------------
# Pool
# ---------------------------------------------------------------------------

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


def _as_port_int(value):
    return isinstance(value, int) and not isinstance(value, bool) and PORT_MIN <= value <= PORT_MAX


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
    """Legacy: find harbor.toml / .harbor.toml in a project root (migration only)."""
    for name in CONFIG_NAMES:
        candidate = os.path.join(root, name)
        if os.path.isfile(candidate):
            return candidate
    return None


def resolve_toml_arg(path):
    """Accept a config TOML path or a project directory containing one."""
    if os.path.isdir(path):
        found = locate_config(path)
        if found is None:
            raise PoolError(f"no harbor.toml or .harbor.toml in directory {path}")
        return found
    if not os.path.isfile(path):
        raise PoolError(f"not a file or directory: {path}")
    return path


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


# ---------------------------------------------------------------------------
# Registration
# ---------------------------------------------------------------------------

def _sync_mirror(root, projects_dir=None, projects_path=None, remove=False):
    """Keep the pre-1.3 projects.json mirror in sync with the central store.

    Only the default store syncs the mirror (or an explicit --projects-json
    path); a custom projects_dir is a test/isolated store without a mirror.
    """
    if projects_path is None:
        if projects_dir is not None:
            return
        projects_path = DEFAULT_PROJECTS_JSON
    roots = []
    if os.path.isfile(projects_path):
        try:
            with open(projects_path, encoding="utf-8") as fh:
                data = json.load(fh)
            if isinstance(data, list):
                roots = [
                    os.path.normpath(os.path.expanduser(entry))
                    if isinstance(entry, str) else entry
                    for entry in data
                ]
        except (OSError, json.JSONDecodeError):
            roots = []
    if remove:
        roots = [entry for entry in roots if entry != root]
    elif root not in roots:
        roots.append(root)
    else:
        return  # already in sync; leave the file untouched
    _atomic_write(projects_path, json.dumps(roots, indent=2) + "\n")


def _validate_draft(text, root, projects_dir=None, exclude=None):
    """Structural check + cross-project port scan before installing.

    Raises PoolError on any validator error or static port overlap with
    another registered project.
    """
    import tempfile
    from validate_harbor_toml import check_file

    with tempfile.NamedTemporaryFile("w", suffix=".toml", encoding="utf-8", delete=False) as fh:
        fh.write(text)
        draft_path = fh.name
    try:
        pool_ranges = load_port_pool(missing_ok=True)
        _, ports, errors, warnings = check_file(draft_path, pool_ranges)
        if errors:
            raise PoolError("config rejected: " + " | ".join(errors))
        taken: dict[int, str] = {}
        for path in list_central_configs(projects_dir):
            if exclude is not None and os.path.abspath(path) == os.path.abspath(exclude):
                continue
            holder = os.path.basename(path)
            for port in claimed_ports_from_toml(path):
                taken.setdefault(port, holder)
        clashes = sorted(port for port in ports if port in taken)
        if clashes:
            detail = ", ".join(f"{port} ({taken[port]})" for port in clashes)
            raise PoolError(f"port(s) already claimed by other projects: {detail}")
        del warnings
    finally:
        try:
            os.remove(draft_path)
        except OSError:
            pass


def register_project(root, config_text=None, config_path=None,
                     projects_dir=None, projects_path=None):
    """Install a config into ~/.harbor/projects/ as the project's definition.

    The project root never needs a harbor.toml anymore — the central store
    is the single source of truth and Harbor watches it live. The config
    comes from `config_text`, a draft file via `config_path`, or stdin.
    `register_project.py` runs the same migration as the app first, then
    validates the draft (structure + cross-project port scan) and writes
    the central file atomically; the pre-1.3 projects.json mirror is kept
    in sync.

    Returns "registered" (new file), "updated" (content changed), or
    "unchanged" (identical content already installed).
    """
    root = _validated_abspath(root, "project root")
    if not os.path.isdir(root):
        raise PoolError(f"not a directory: {root}")

    if config_text is None and config_path is None:
        if sys.stdin is None or sys.stdin.isatty():
            raise PoolError("no config given — pass --config FILE or pipe TOML on stdin")
        config_text = sys.stdin.read()
    elif config_path is not None:
        if not os.path.isfile(config_path):
            raise PoolError(f"config file not found: {config_path}")
        with open(config_path, encoding="utf-8") as fh:
            config_text = fh.read()
    if not isinstance(config_text, str) or not config_text.strip():
        raise PoolError("config is empty")

    directory = projects_dir or PROJECTS_DIR
    if projects_dir is None:
        migrate_legacy_configs()

    content = _compose_config(config_text, root)
    existing = find_central_config_for_root(root, directory)
    if existing is not None:
        try:
            with open(existing, encoding="utf-8") as fh:
                current = fh.read()
        except OSError:
            current = None
        if current == content:
            _sync_mirror(root, projects_dir=projects_dir, projects_path=projects_path)
            return "unchanged"

    _validate_draft(content, root, projects_dir=directory, exclude=existing)
    install_config(config_text, root, projects_dir=directory, replace=existing)
    _sync_mirror(root, projects_dir=projects_dir, projects_path=projects_path)
    return "updated" if existing is not None else "registered"


def unregister_project(root, projects_dir=None, projects_path=None):
    """Remove the central config declaring `root` — deleting the file IS
    unregistering; Harbor's directory watcher drops the project live.

    Returns "unregistered" or "not-registered".
    """
    root = _validated_abspath(root, "project root")
    directory = projects_dir or PROJECTS_DIR
    target = find_central_config_for_root(root, directory)
    if target is None:
        return "not-registered"
    os.remove(target)
    if projects_dir is None:
        migrate_legacy_configs()  # no-op in practice; keeps ordering simple
    _sync_mirror(root, projects_dir=projects_dir, projects_path=projects_path, remove=True)
    return "unregistered"
