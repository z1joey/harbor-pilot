---
name: harbor-toml
description: Generate and validate harbor.toml configs for Harbor, the macOS menubar dev-process manager. Use whenever the user wants to add/register a project to Harbor, create or fix a harbor.toml or .harbor.toml, declare processes/ports/port claims, or asks about avoiding port conflicts between their local projects while configuring one — even if they just say "接入 harbor"、"写个 harbor 配置"、"把这个项目加到 Harbor".
---

# Harbor: generate harbor.toml

Harbor is a macOS menubar app that supervises per-project dev processes
declared in a `harbor.toml` (or `.harbor.toml`) at the project root. This
skill produces configs the real parser accepts, with ports planned so that
multiple projects don't collide.

## Workflow

1. **Discover what the project runs.** Read `package.json` (scripts),
   `Procfile`, `docker-compose.yml` / `compose.yaml`, `.env`, and framework
   configs (`vite.config.*`, `application.yml`, `settings.py`, …). Only ask
   the user about what you cannot discover this way.
2. **Collect ports already claimed** by other Harbor projects (see *Port
   planning* below). Never hand out a fixed port that another project claims.
3. **Choose auto vs fixed per process** using the table in *port vs
   port_claim vs port = "auto"* below. Default to `port = "auto"` for
   standalone dev servers nothing else connects to; use a **fixed** port when
   another process, proxy (`vite.config` → `server.proxy`), or config file
   references this port by number.
4. **Draft the config** following the schema below — exact key names matter.
5. **Validate**: run
   `python3 <this-skill-dir>/scripts/validate_harbor_toml.py <draft.toml> <other-projects' *.toml ...>`
   and fix everything it reports. Passing the other projects' configs lets it
   catch cross-project port overlaps too.
6. **Tell the user how to register**: Harbor app → main window →
   "Add Project…" → pick the folder. Harbor watches the file, so later edits
   hot-reload.

## Schema (parser-exact)

```toml
name = "my-app"                  # optional; defaults to the folder name
open_process = "web"             # optional; project "Open in Browser" target (must match a [[process]] name)
# open_url = "http://127.0.0.1:8080/"  # optional static URL; mutually exclusive with open_process

[[process]]                      # one block per managed process
name = "api"                     # required; unique within the project
command = "uv run uvicorn app.main:app --reload --port 8000"  # fixed port: hardcode N in command
cwd = "backend"                  # optional; relative to project root
port = 8000                      # optional: fixed int 1–65535, or "auto" (see below)
ready_url = "http://127.0.0.1:8000/health"  # optional; use ${port} only with port = "auto"
# Auto alternative: command = "... --port $PORT", port = "auto",
# ready_url = "http://127.0.0.1:${port}/health", port_env = "PORT"
auto_restart = false             # optional; restart on unexpected exit (default false)
env = { "FOO" = "bar", "RETRIES" = 3 }      # optional; int/bool coerced to strings

[[port_claim]]
port = 5432                      # required; 1–65535
note = "postgres"                # optional; shown in the ports overview
process = "deps"                 # optional; must match a [[process]] name above
```

Parser rules it enforces (an invalid config still registers but shows an
error banner and zero processes):

- Process `name`: required, non-empty, unique in the project.
- `command`: required, non-empty, run as a foreground long-running process
  via a login shell (`/bin/zsh -lc`) — so `npm`, `uv`, `nvm` PATHs all work.
- `port`: optional. Either a fixed integer (1–65535) or the string `"auto"`.
  - **Fixed port** — conflict detection, browser link, and pre-start overlap
    warnings use this number. Use when the app hardcodes its port in several
    places and you want Harbor to plan around a known value.
  - **`port = "auto"`** — Harbor picks a free port from **8100–9999** on
    each start (nothing persisted; the number changes every run). Harbor
    injects the chosen port as the `PORT` environment variable (override the
    name with `port_env`). Reference it in `command` via `$PORT` or
    `${PORT}`. Auto ports skip pre-start conflict checks; Harbor scans at
    start time instead. Does not affect deployment — only applies to
    processes Harbor spawns locally.
- `port_env`: optional string, only allowed with `port = "auto"`. Must match
  `[A-Za-z_][A-Za-z0-9_]*`. Default `PORT`. Harbor's assignment wins over
  a conflicting value in `env`.
- `ready_url`: optional URL with a scheme. May contain `${port}` **only**
  when `port = "auto"` (substituted at start with the assigned port).
- `port_claim`: ports the project relies on without one owning process —
  databases, brokers, or a compose process exposing several ports. Same port
  must not appear twice in the project (claim vs claim, or claim vs process
  `port`), and `process` must reference a defined process.
- Two projects claiming the same **fixed** port is legal TOML but creates a
  *static overlap*: Harbor warns when adding the project, again at start
  time, and flags it in the Ports Overview. Plan ports to avoid it.
- `open_process` / `open_url`: optional, mutually exclusive. Enables the
  menubar safari button and main-window **Open in Browser** for the whole
  project. Prefer `open_process` for full-stack apps (e.g. `open_process =
  "frontend"`) so Harbor resolves `ready_url` or `http://127.0.0.1:<port>/`
  with live auto-port assignment. Use `open_url` only for a fixed bookmark.
  Do not point at API health endpoints (`/health`) when the user expects the UI.

## port vs port_claim vs port = "auto"

- The process itself listens on a **known** port → fixed `port = N` on that
  `[[process]]`.
- The process should get a **free port each run** → `port = "auto"` and
  `$PORT` in `command`. Best when two projects would otherwise share a
  framework default (two Vite apps both on 5173).
- Something else in the project's stack uses it (containerized postgres,
  redis, a second socket) → `[[port_claim]]`.

When choosing auto vs fixed:

| Situation | Prefer |
|---|---|
| Two+ projects, same framework default | `port = "auto"` + `$PORT` in command |
| Another process or proxy config references this port | Fixed `port = N` (e.g. Vite `server.proxy` → API on 8001) |
| Port hardcoded in vite.config, compose, `.env`, etc. | Fixed `port = N` matching those files |
| OAuth redirect URIs / bookmarks need a stable local port | Fixed `port = N` (auto changes every start) |
| Server already reads `process.env.PORT` / `$PORT` | `port = "auto"` — minimal config change |

## Automatic ports example

```toml
name = "my-api"

[[process]]
name = "api"
command = "uv run uvicorn app.main:app --reload --port $PORT"
port = "auto"
ready_url = "http://127.0.0.1:${port}/health"
```

Common command patterns:

| Stack | command with auto port |
|---|---|
| uvicorn | `uv run uvicorn app.main:app --reload --port $PORT` |
| python http.server | `python3 -m http.server $PORT` |
| Vite | `npm run dev -- --port $PORT` |
| Next.js | `npm run dev -- -p $PORT` |
| Rails | `bin/rails server -p $PORT` |
| Node (reads env) | `npm run dev` (if script uses `process.env.PORT`) |

Harbor shows a soft lint when `port = "auto"` but the command string does not
reference `$PORT`. After ~5s it badges a mismatch if the process tree
listens on a port other than the one Harbor assigned.

**Deployment note:** Harbor only injects `PORT` into processes it spawns from
`harbor.toml`. Production deploys (Docker, CI, PaaS) are unaffected. If you
move `--port $PORT` into a committed npm script, add a default there
(`${PORT:-3000}`) so CI without Harbor still starts.

## Port planning

1. Registered projects: read
   `~/Library/Application Support/Harbor/projects.json` (a JSON array of
   absolute paths). Read each path's `harbor.toml` / `.harbor.toml` and
   collect every **fixed** process `port` and `port_claim` port — these are
   taken. `port = "auto"` processes contribute no static claim.
2. Ports listening right now: `lsof -nP -iTCP -sTCP:LISTEN +c0` — treat as
   taken as well (some may belong to projects not yet registered).
3. For fixed ports, suggest numbers from **8000–8099** first, then **10000+**,
   skipping both sets — avoid **8100–9999** for fixed assignments (Harbor's
   auto pool). Harbor's auto allocator scans 8100–9999 at start time.
4. Remember: Harbor will not rewrite the app's own configs. If the project
   hardcodes its port in five places (Dockerfile, compose, nginx, proxy,
   dev script), either use `port = "auto"` **and** wire `$PORT` through
   those entry points, or pick a fixed `port = N` that matches them — flag
   any mismatch instead of silently diverging.

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
default — use `port = "auto"` or renumber deliberately.

## Complete example

Full-stack project: Vite frontend, FastAPI backend, Postgres + Redis in
Docker via compose:

```toml
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
port = "auto"
```

Notes:

- `docker compose up` **without** `-d` keeps the containers as a supervised
  foreground tree — Harbor can stop them cleanly; their ports are declared
  as `port_claim`s.
- Put connection strings that differ per machine in `env`, not in the
  project's committed config.
- `api` stays on a **fixed** port because `frontend/vite.config` proxies to
  `http://127.0.0.1:8001` — Harbor has no cross-process port references, so
  anything another process reaches by number must be fixed. `web` uses
  `port = "auto"` so multiple Vite projects don't collide on 5173.
