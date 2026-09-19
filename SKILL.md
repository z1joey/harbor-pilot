---
name: harbor-pilot
description: Assign sticky dev-server ports and register projects into Harbor's central store at ~/.harbor — fully standalone, with or without the Harbor app running. Use whenever the user wants to add/register a project to Harbor, create or fix a Harbor config (harbor.toml or the ~/.harbor/projects store), declare processes/ports/port claims, or asks about avoiding port conflicts between their local projects while configuring one — even if they just say "接入 harbor"、"写个 harbor 配置"、"把这个项目加到 Harbor".
---

# Harbor Pilot: assign ports & register projects in ~/.harbor

Harbor Pilot is a **standalone agent skill** for planning dev-server ports
and registering projects in Harbor's convention. It hands out sticky ports
from the port pool so multiple projects don't collide, and **registers the
project** by installing its config into Harbor's central store.

**No app required — and no files in the project.** Since Harbor **1.3.0**
the single source of truth is the hidden `~/.harbor` folder; Harbor never
reads a `harbor.toml` from the project root anymore. The Harbor menubar app
and `harbor-tui` are optional consumers: they only read (and watch) the same
files. Everything this skill does — port planning, validation,
registration — works with no Harbor process running, and
`register_project.py` creates `~/.harbor` on demand (pool defaults to
8100–8199).

Harbor's on-disk state is the hidden `~/.harbor` folder:

```
~/.harbor/projects/<name>.toml   # one config per project — the directory
                                 # listing IS the registry (this skill writes it)
~/.harbor/projects.json          # derived mirror of roots for pre-1.3 consumers
~/.harbor/port-pool.json         # { "ranges": [{ "from": 8100, "to": 8199 }] }
```

Each central config carries a top-level `root = "/absolute/path"` key naming
the project folder; Harbor runs the commands there. Legacy state (App
Support stores, per-root `harbor.toml` listed in `projects.json`) is
migrated automatically — by the app at startup and by this skill before its
first registration. Root-side `harbor.toml` files are then ignored: they are
safe to `git rm`.

## Workflow

1. **Discover what the project runs.** Read `package.json` (scripts),
   `Procfile`, `docker-compose.yml` / `compose.yaml`, `.env`, and framework
   configs (`vite.config.*`, `application.yml`, `settings.py`, …). Only ask
   the user about what you cannot discover this way.
2. **Collect ports already claimed** by other Harbor projects (see *Port
   planning* below). Never hand out a port that another project already
   declares.
3. **Register from the pool** for each Harbor-managed server that does not
   already have a hardcoded number elsewhere (vite proxy, `.env`, OAuth
   redirect). Run `scripts/next_pool_port.py --lsof --registered`, skip
   taken ports, write sticky `port = N` with `$PORT` in `command` /
   `${port}` in `ready_url`. If another config **hardcodes** a port, use
   that integer even when it sits outside the pool, and flag mismatches.
   `[[port_claim]]` is unchanged (deps stay outside the pool). Never write
   `port = "auto"`.
4. **Draft the config** following the schema below — exact key names matter.
   The draft can be any file (e.g. `/tmp/<name>-harbor.toml`); do not write
   into the project.
5. **Validate**: run
   `python3 <this-skill-dir>/scripts/validate_harbor_toml.py <draft.toml>`
   and fix everything it reports. Pass other central configs as extra
   arguments to catch cross-project port overlaps too. Use
   `scripts/next_pool_port.py` (below) instead of reimplementing the scan.
6. **Register**: run
   `python3 <this-skill-dir>/scripts/register_project.py <project-root> --config <draft.toml>`.
   It injects the `root` key, validates again, and installs the config as
   `~/.harbor/projects/<name>.toml` (atomic, idempotent: REGISTERED /
   UPDATED / UNCHANGED). A running Harbor shows the project within a
   second — there is no "Add Project…" step anymore; with no frontend
   running it appears at next launch.

## Schema (parser-exact)

```toml
root = "/Users/joey/Projects/my-app"   # required in ~/.harbor/projects/*.toml;
                                       # injected by register_project.py — optional in drafts
name = "my-app"                        # optional; defaults to the root folder name;
                                       # also becomes the config filename
open_process = "web"                   # optional; project "Open in Browser" target (must match a [[process]] name)
# open_url = "http://127.0.0.1:8080/"  # optional static URL; mutually exclusive with open_process

[[process]]                      # one block per managed process
name = "api"                     # required; unique within the project
command = "uv run uvicorn app.main:app --reload --port $PORT"
cwd = "backend"                  # optional; relative to root
port = 8100                      # optional: sticky integer 1–65535 (pool registration)
ready_url = "http://127.0.0.1:${port}/health"  # optional; ${port} substitutes N
# port_env = "PORT"              # optional env var name Harbor injects at start (default PORT)
auto_restart = false             # optional; restart on unexpected exit (default false)
env = { "FOO" = "bar", "RETRIES" = 3 }      # optional; int/bool coerced to strings

[[port_claim]]
port = 5432                      # required; 1–65535
note = "postgres"                # optional; shown in Port Allocation Convention
process = "deps"                 # optional; must match a [[process]] name above
```

Parser rules it enforces (an invalid config still registers but shows an
error banner and zero processes):

- `root`: required in the central store. Absolute path, `~` allowed. Two
  configs claiming the same root is an error — the second file (by
  filename) is flagged until the duplicate is removed.
- Process `name`: required, non-empty, unique in the project.
- `command`: required, non-empty, run as a foreground long-running process
  via a login shell (`/bin/zsh -lc`) — so `npm`, `uv`, `nvm` PATHs all work.
- `port`: optional integer **1–65535**. `"auto"` is a parse error. The
  number is a sticky lease Harbor injects at start as `port_env` (default
  `PORT`); it does not change between runs.
  - **Pool registration** — for Harbor-managed servers nothing else
    hardcodes, pick the next free port from the pool and write `port = N`
    with `$PORT` / `${PORT}` in `command`.
  - **Hardcoded outside the pool** — legal. Use when vite proxy, `.env`,
    OAuth redirects, or another process already pin a number (`5173`,
    `8001`, …). Match those files; flag mismatches instead of silently
    diverging.
- `port_env`: optional string, allowed on any process that declares
  `port`. Must match `[A-Za-z_][A-Za-z0-9_]*`. Default `PORT`. Harbor's
  injected value wins over a conflicting key in `env`.
- `ready_url`: optional URL with a scheme. May contain `${port}` whenever
  `port` is declared (substituted with `N` at start).
- `port_claim`: ports the project relies on without one owning process —
  databases, brokers, or a compose process exposing several ports. Same port
  must not appear twice in the project (claim vs claim, or claim vs process
  `port`), and `process` must reference a defined process. Claims are **not**
  pool leases; leave typical DB/broker ports here.
- Two projects claiming the same **fixed** port is legal TOML but creates a
  *static overlap*: Harbor warns at start time and flags it in Port
  Allocation Convention — and `register_project.py` refuses the
  registration until it is resolved. Plan ports to avoid it.
- `open_process` / `open_url`: optional, mutually exclusive. Enables the
  menubar safari button and main-window **Open in Browser** for the whole
  project. Prefer `open_process` for full-stack apps (e.g. `open_process =
  "frontend"`) so Harbor resolves `ready_url` or `http://127.0.0.1:<port>/`
  using the sticky port. Use `open_url` only for a fixed bookmark.
  Do not point at API health endpoints (`/health`) when the user expects the UI.

## port vs port_claim vs pool registration

- Harbor-managed server, nothing else pins the number → **register** the
  next free **pool** port on that `[[process]]` (`port = N` + `$PORT`).
- Another config already hardcodes the number (proxy, `.env`, OAuth) → fixed
  `port = N` matching that file, even outside the pool.
- Something else in the project's stack uses it (containerized postgres,
  redis, a second socket) → `[[port_claim]]`. Unchanged; keep these outside
  the pool.

| Situation | Prefer |
|---|---|
| Two+ projects, same framework default | Pool registration + `$PORT` in command |
| Another process or proxy config references this port | Fixed `port = N` matching that config (may be outside the pool) |
| Port hardcoded in vite.config, compose, `.env`, etc. | Fixed `port = N` matching those files |
| OAuth redirect URIs / bookmarks need a stable local port | Pool registration (`port = N` is sticky across restarts) |
| Server already reads `process.env.PORT` / `$PORT` | Pool registration — Harbor injects `PORT=N` |

## Port planning

1. **Pool** — read `~/.harbor/port-pool.json`. If the file
   is missing, use **8100–8199**. Shape:

   ```json
   { "ranges": [{ "from": 8100, "to": 8199 }] }
   ```

   One or more inclusive ranges; Harbor rejects inverted, overlapping, or
   out-of-`1…65535` ranges. Users edit this in **Port Allocation
   Convention**; the skill only reads it. (Pre-1.2 Harbor kept the file in
   `~/Library/Application Support/Harbor/` — the scripts fall back to that
   path while only it exists.)

2. **Taken** — registered projects:
   `~/.harbor/projects/*.toml` (one config per project). Collect every
   process `port` and `port_claim` port:

   ```bash
   python3 <this-skill-dir>/scripts/next_pool_port.py --lsof --registered
   ```

3. **Register** — assign the first free pool port to each Harbor-managed
   server that does not already have a hardcoded number elsewhere. Write
   `port = N` and keep `$PORT` in `command` / `${port}` in `ready_url`.

   Helper (does not need Harbor running):

   ```bash
   python3 <this-skill-dir>/scripts/next_pool_port.py --lsof --registered \
     /path/to/other-draft.toml
   ```

   Prints the next free pool port. Use `--count N` for several servers
   (already-printed numbers are skipped). Extra taken ports can be piped
   with `--stdin-taken` (one integer per line).

4. If another config **hardcodes** a port, use that integer (even outside
   the pool) and flag mismatches — Harbor will not rewrite the app's own
   files (Dockerfile, compose, nginx, proxy, dev script).

5. `[[port_claim]]` unchanged.

Never write `port = "auto"`. The validator rejects it.

## Framework default ports (for discovery)

| Thing | Default |
|---|---|
| Vite | 5173 |
| Next.js / CRA / Rails | 3000 |
| FastAPI / uvicorn / Django | 8000 |
| Flask | 5000 |
| Spring Boot | 8080 |
| Postgres | 5432 |
| MySQL | 3306 |
| Redis | 6379 |
| MongoDB | 27017 |

Two dev servers of the same framework in different projects collide by
default — register each from the pool (or renumber a hardcoded port
deliberately when something else pins it).

## Pool registration example

```toml
root = "/Users/joey/Projects/my-api"   # injected by register_project.py

[[process]]
name = "api"
command = "uv run uvicorn app.main:app --reload --port $PORT"
port = 8100
ready_url = "http://127.0.0.1:${port}/health"
```

`8100` here is an example of the first default-pool port. **Do not copy it
blindly** — run `next_pool_port.py --lsof --registered` so you get the next
free number.

Common command patterns:

| Stack | command with `$PORT` |
|---|---|
| uvicorn | `uv run uvicorn app.main:app --reload --port $PORT` |
| python http.server | `python3 -m http.server $PORT` |
| Vite | `npm run dev -- --port $PORT` |
| Next.js | `npm run dev -- -p $PORT` |
| Rails | `bin/rails server -p $PORT` |
| Node (reads env) | `npm run dev` (if script uses `process.env.PORT`) |

Harbor shows a soft lint when `port` is set but the command string
references neither `$PORT` / `${PORT}` (or `port_env`) nor the decimal `N`.
After ~5s it badges a mismatch if the process tree listens on a port other
than the one declared.

**Deployment note:** Harbor only injects `PORT` into processes it spawns
from its configs. Production deploys (Docker, CI, PaaS) are unaffected. If
you move `--port $PORT` into a committed npm script, add a default there
(`${PORT:-3000}`) so CI without Harbor still starts.

## Complete example

Full-stack project: Vite frontend, FastAPI backend, Postgres + Redis in
Docker via compose. `web` is a pool registration; `api` stays on **8001**
because the frontend proxy hardcodes that number; DB/broker ports are
claims.

Same content lives in `examples/fullstack.toml`.

```toml
root = "/Users/joey/Projects/shop"     # injected by register_project.py
name = "shop"
open_process = "web"

[[process]]
name = "deps"
command = "docker compose up postgres redis"   # foreground, so Harbor can stop it
[[port_claim]]
port = 5432
note = "postgres"
process = "deps"
[[port_claim]]
port = 6379
note = "redis"
process = "deps"

[[process]]
name = "api"
command = "uv run uvicorn app.main:app --reload --port 8001"
cwd = "backend"
port = 8001
ready_url = "http://127.0.0.1:8001/health"
env = { "DATABASE_URL" = "postgres://localhost/shop" }

[[process]]
name = "web"
command = "npm run dev -- --port $PORT"
cwd = "frontend"
port = 8100
ready_url = "http://127.0.0.1:${port}/"
```

Notes:

- `docker compose up` **without** `-d` keeps the containers as a supervised
  foreground tree — Harbor can stop them cleanly; their ports are declared
  as `port_claim`s.
- Put connection strings that differ per machine in `env`, not in the
  config.
- `api` stays on a **fixed** port because `frontend/vite.config` proxies to
  `http://127.0.0.1:8001` — Harbor has no cross-process port references, so
  anything another process reaches by number must be fixed (and may sit
  outside the pool). `web` uses a **pool** port so multiple Vite projects
  don't collide on 5173; substitute `8100` with whatever
  `next_pool_port.py` prints.

## Registering / unregistering

- Register: `python3 <this-skill-dir>/scripts/register_project.py <root> --config <draft.toml>`
  (idempotent; validates the draft, injects `root`, installs
  `~/.harbor/projects/<name>.toml` atomically; the app watches the central
  directory and shows the project immediately). A stdin draft works too:
  `... <root> < draft.toml`.
- Unregister: `python3 <this-skill-dir>/scripts/unregister_project.py <root>`
  — deleting the central config file IS unregistering; Harbor hot-reloads
  it. Nothing inside the project root is ever touched.
- Fixing a config: edit `~/.harbor/projects/<name>.toml` directly (or
  re-run `register_project.py` with a new draft — it UPDATES in place).
  Removing an entry never deletes anything on disk.
- Legacy migration: roots listed in the pre-1.3 `projects.json` get their
  root `harbor.toml` imported into the central store automatically
  (by the app at startup and before the first registration). The old files
  are then ignored and safe to `git rm`.
