# harbor-pilot

A **standalone agent skill** (Cursor & friends) for [Harbor](https://github.com/z1joey/harbor) **1.3.0** — drafts and validates Harbor configs, **assigns sticky ports from Harbor's port pool**, plans claims across registered projects, and **registers the project in `~/.harbor/projects/`**.

**Works without the app — and never writes into your project.** Since Harbor 1.3.0 the single source of truth is the hidden `~/.harbor` folder: one config TOML per project under `projects/` (the directory listing IS the registry), the pool in `port-pool.json`, and a derived `projects.json` mirror for pre-1.3 consumers. Each central config carries `root = "/absolute/path"` naming the project folder; Harbor runs the commands there and never reads the project for config. The Harbor app and `harbor-tui` are optional consumers that only read and watch those files. With no `~/.harbor` yet, `register_project.py` creates it (pool defaults **8100–8199**). Managed servers get a durable `port = N`; Harbor injects `$PORT` at start when it launches them — the skill picks the number either way.

## Install

```bash
mkdir -p ~/.agents/skills
git clone https://github.com/z1joey/harbor-pilot.git ~/.agents/skills/harbor-pilot
```

In Cursor chat, attach or invoke the **harbor-pilot** skill and ask e.g. *"接入 harbor，帮我配置一下"* or *"Add `~/Projects/my-app` to Harbor"*.

## Next free pool port

```bash
python3 ~/.agents/skills/harbor-pilot/scripts/next_pool_port.py \
  --lsof \
  --registered \
  path/to/other-draft.toml
```

Prints the next free port in the configured pool, skipping process `port` and `port_claim` values in the given TOMLs and — with `--registered` — in every central config under `~/.harbor/projects/`. `--count N` prints N ports. Missing pool file → 8100–8199.

## Register a project

```bash
python3 ~/.agents/skills/harbor-pilot/scripts/register_project.py \
  ~/Projects/my-app --config /tmp/my-app-harbor.toml
```

Validates the draft (structure + cross-project port scan), injects/updates the `root` key, and installs it as `~/.harbor/projects/<name>.toml` (atomic, idempotent: `REGISTERED` / `UPDATED` / `UNCHANGED`; a stdin draft works too). The project root itself never needs a `harbor.toml`. A running Harbor app/TUI picks the project up within a second; with nothing running it simply appears at next launch.

## Unregister a project

```bash
python3 ~/.agents/skills/harbor-pilot/scripts/unregister_project.py ~/Projects/my-app
```

Deletes the central config — deleting the file IS unregistering; Harbor hot-reloads it. Nothing inside the project root is ever touched.

## Validate without the app

```bash
python3 ~/.agents/skills/harbor-pilot/scripts/validate_harbor_toml.py \
  /tmp/my-app-harbor.toml \
  ~/.harbor/projects/other.toml
```

Exit `0` = OK; `1` = errors/overlaps; `2` = missing Python TOML library (`tomli` on Python &lt; 3.11) or usage. A file still named `harbor.toml` / `.harbor.toml` in a project root is flagged: Harbor 1.3+ no longer reads project roots (safe to `git rm` after migration).

`port = "auto"` is an error. `port_env` and `${port}` are valid with a fixed `port`. `OVERLAP` is still an error when two configs share a port. Optional `WARN` if a managed server uses `$PORT` with a port outside the pool.

## Contents

| Path | Purpose |
|------|---------|
| `SKILL.md` | Full workflow, schema, pool-registration SOP for the agent |
| `examples/fullstack.toml` | Full-stack sample: pool `web` + hardcoded `api` + DB claims |
| `scripts/next_pool_port.py` | Pool file + central store → next free port |
| `scripts/register_project.py` | Validate + install a central config for a project root |
| `scripts/unregister_project.py` | Remove a project's central config |
| `scripts/harbor_pool.py` | Shared central-store + pool JSON + claimed-port helpers |
| `scripts/validate_harbor_toml.py` | Parser-aligned validator + overlap checks + pool warnings |
| `scripts/test_port_pool.py` | Unit + CLI tests for all of the above |

The skill writes only under `~/.harbor/` (and the draft you hand it); it never touches `port-pool.json` (users edit that in the app's Port Convention) and never adds files to project repos. Legacy state — pre-1.2 App Support stores and per-root `harbor.toml` files listed in `projects.json` — is migrated into the central store automatically on first registration (and by the app at launch); old root files are then ignored and safe to `git rm`.
