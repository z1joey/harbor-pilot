---
name: harbor-toml
description: Generate and validate harbor.toml configs for Harbor, the macOS menubar dev-process manager. Use whenever the user wants to add/register a project to Harbor, create or fix a harbor.toml or .harbor.toml, declare processes/ports/port claims, or asks about avoiding port conflicts between their local projects while configuring one — even if they just say "接入 harbor"、"写个 harbor 配置"、"把这个项目加到 Harbor".
---

# Harbor: generate harbor.toml

Harbor is a macOS menubar app that supervises per-project dev processes
declared in a `harbor.toml` (or `.harbor.toml`) at the project root. This
skill produces configs the real parser accepts (Harbor **1.1.0**), and
**registers** sticky ports from Harbor's port pool so multiple projects
don't collide.

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
   redirect). Read `port-pool.json` (default **8100–8199** if missing), skip
   taken ports, write sticky `port = N` with `$PORT` in `command` /
   `${port}` in `ready_url`. If another config **hardcodes** a port, use
   that integer even when it sits outside the pool, and flag mismatches.
   `[[port_claim]]` is unchanged (deps stay outside the pool). Never write
   `port = "auto"`.
4. **Draft the config** following the schema below — exact key names matter.
5. **Validate**: run
   `python3 <this-skill-dir>/scripts/validate_harbor_toml.py <draft.toml> <other-projects' *.toml ...>`
   and fix everything it reports. Passing the other projects' configs lets it
   catch cross-project port overlaps too. Use
   `scripts/next_pool_port.py` (below) instead of reimplementing the scan.
6. **Tell the user how to register**: Harbor app → main window →
   "Add Project…" → pick the folder. Harbor watches the file, so later edits
   hot-reload. Do not rewrite `projects.json` or `port-pool.json`.

## Schema (parser-exact)

```toml
name = "my-app"                  # optional; defaults to the folder name
open_process = "web"             # optional; project "Open in Browser" target (must match a [[process]] name)
# open_url = "http://127.0.0.1:8080/"  # optional static URL; mutually exclusive with open_process

[[process]]                      # one block per managed process
name = "api"                     # required; unique within the project
command = "uv run uvicorn app.main:app --reload --port $PORT"
cwd = "backend"                  # optional; relative to project root
port = 8100                      # optional: sticky integer 1–65535 (pool registration)
ready_url = "http://127.0.0.1:${port}/health"  # optional; ${port} substitutes N
# port_env = "PORT"              # optional env var name Harbor injects (default PORT)
auto_restart = false             # optional; restart on unexpected exit (default false)
env = { "FOO" = "bar", "RETRIES" = 3 }      # optional; int/bool coerced to strings

[[port_claim]]
port = 5432                      # required; 1–65535
note = "postgres"                # optional; shown in Port Allocation Convention
process = "deps"                 # optional; must match a [[process]] name above
```

Parser rules it enforces (an invalid config still registers but shows an
error banner and zero processes):

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
  *static overlap*: Harbor warns when adding the project, again at start
  time, and flags it in Port Allocation Convention. Plan ports to avoid it.
- `open_process` / `open_url`: optional, mutually exclusive. Enables the
  menubar safari button and main-window **Open in Browser** for the whole
  project. Prefer `open_process` for full-stack apps (e.g. `open_process =
  "frontend"`) so Harbor resolves `ready_url` or `http://127.0.0.1:<port>/`
  using the sticky port. Use `open_url` only for a fixed bookmark.
  Do not point at API health endpoints (`/health`) when the user expects the UI.

## port vs port_claim vs pool registration

- Harbor-managed server, nothing else pins the number → **register** the
  next free **pool** port on that `[[process]]` (`port = N` + `$PORT`).
- Another config already hardcodes the number (proxy, `.env`, OAuth) →
  fixed `port = N` matching that file, even outside the pool.
- Something else in the project's stack uses it (containerized postgres,
  redis, a second socket) → `[[port_claim]]`. Unchanged; keep these
  outside the pool.

| Situation | Prefer |
|---|---|
| Two+ projects, same framework default | Pool registration + `$PORT` in command |
| Another process or proxy config references this port | Fixed `port = N` matching that config (may be outside the pool) |
| Port hardcoded in vite.config, compose, `.env`, etc. | Fixed `port = N` matching those files |
| OAuth redirect URIs / bookmarks need a stable local port | Pool registration (`port = N` is sticky across restarts) |
| Server already reads `process.env.PORT` / `$PORT` | Pool registration — Harbor injects `PORT=N` |

## Pool registration example

```toml
name = "my-api"

[[process]]
name = "api"
command = "uv run uvicorn app.main:app --reload --port $PORT"
port = 8100
ready_url = "http://127.0.0.1:${port}/health"
```

`8100` here is an example of the first default-pool port. **Do not copy it
blindly** — run `next_pool_port.py` against sibling TOMLs so you get the
next free number.

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

**Deployment note:** Harbor only injects `PORT` into processes it spawns from
`harbor.toml`. Production deploys (Docker, CI, PaaS) are unaffected. If you
move `--port $PORT` into a committed npm script, add a default there
(`${PORT:-3000}`) so CI without Harbor still starts.

## Port planning

1. **Pool** — read
   `~/Library/Application Support/Harbor/port-pool.json`. If the file is
   missing, use **8100–8199**. Shape:

   ```json
   { "ranges": [{ "from": 8100, "to": 8199 }] }
   ```

   One or more inclusive ranges; Harbor rejects inverted, overlapping, or
   out-of-`1…65535` ranges. Users edit this in **Port Allocation
   Convention**; the skill only reads it.

2. **Taken** — registered projects:
   `~/Library/Application Support/Harbor/projects.json` (a JSON array of
   absolute paths). Read each path's `harbor.toml` / `.harbor.toml` and
   collect every process `port` and `port_claim` port. Optionally treat
   live listeners as taken too:
   `lsof -nP -iTCP -sTCP:LISTEN +c0`.

3. **Register** — assign the first free pool port to each Harbor-managed
   server that does not already have a hardcoded number elsewhere. Write
   `port = N` and keep `$PORT` in `command` / `${port}` in `ready_url`.

   Helper (does not need Harbor running):

   ```bash
   python3 <this-skill-dir>/scripts/next_pool_port.py --lsof \
     --projects-json \
     /path/to/other-project/harbor.toml
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

## Complete example

Full-stack project: Vite frontend, FastAPI backend, Postgres + Redis in
Docker via compose. `web` is a pool registration; `api` stays on **8001**
because the frontend proxy hardcodes that number; DB/broker ports are
claims.

Same content lives in `examples/fullstack.toml`.

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
port = 8100
ready_url = "http://127.0.0.1:${port}/"
```

Notes:

- `docker compose up` **without** `-d` keeps the containers as a supervised
  foreground tree — Harbor can stop them cleanly; their ports are declared
  as `port_claim`s.
- Put connection strings that differ per machine in `env`, not in the
  project's committed config.
- `api` stays on a **fixed** port because `frontend/vite.config` proxies to
  `http://127.0.0.1:8001` — Harbor has no cross-process port references, so
  anything another process reaches by number must be fixed (and may sit
  outside the pool). `web` uses a **pool** port so multiple Vite projects
  don't collide on 5173; substitute `8100` with whatever
  `next_pool_port.py` prints.
