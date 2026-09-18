# harbor-toml

Cursor agent skill for [Harbor](https://github.com/z1joey/harbor) **1.1.0** — drafts and validates `harbor.toml` configs from your repo layout, **registers sticky ports from Harbor's port pool**, plans claims across registered projects, and catches schema mistakes before you add a folder in the app.

Harbor 1.1.0 removed `port = "auto"`. Managed servers get a durable `port = N` from the pool (`~/Library/Application Support/Harbor/port-pool.json`, default **8100–8199**). Harbor injects `$PORT` at start; the skill picks the number.

## Install

```bash
mkdir -p ~/.agents/skills
git clone https://github.com/z1joey/harbor-toml.git ~/.agents/skills/harbor-toml
```

In Cursor chat, attach or invoke the **harbor-toml** skill and ask e.g. *"接入 harbor，帮我写个 harbor 配置"* or *"Add `~/Projects/my-app` to Harbor — write harbor.toml"*.

## Next free pool port

```bash
python3 ~/.agents/skills/harbor-toml/scripts/next_pool_port.py \
  --lsof \
  --projects-json \
  path/to/other-project/harbor.toml
```

Prints the next free port in the configured pool, skipping process `port` and `port_claim` values in the given TOMLs (and, with `--projects-json`, every project listed in `~/Library/Application Support/Harbor/projects.json`). `--count N` prints N ports. Missing pool file → 8100–8199.

## Validate without the app

```bash
python3 ~/.agents/skills/harbor-toml/scripts/validate_harbor_toml.py \
  path/to/harbor.toml \
  path/to/other-project/harbor.toml
```

Pass every registered project's config to catch static port overlaps. Exit `0` = OK; `1` = errors/overlaps; `2` = missing Python TOML library (`tomli` on Python &lt; 3.11) or usage.

`port = "auto"` is an error. `port_env` and `${port}` are valid with a fixed `port`. `OVERLAP` is still an error when two configs share a port. Optional `WARN` if a managed server uses `$PORT` with a port outside the pool.

## Contents

| Path | Purpose |
|------|---------|
| `SKILL.md` | Full workflow, schema, pool-registration SOP for the agent |
| `examples/fullstack.toml` | Full-stack sample: pool `web` + hardcoded `api` + DB claims |
| `scripts/next_pool_port.py` | Pool file + sibling TOMLs → next free port |
| `scripts/harbor_pool.py` | Shared pool JSON + claimed-port helpers |
| `scripts/validate_harbor_toml.py` | Parser-aligned validator + overlap checks + pool warnings |

The skill writes `harbor.toml` on disk only — register the folder in Harbor via **Add Project…** after saving. Do not rewrite `projects.json` or `port-pool.json`.
