# harbor-toml

Cursor agent skill for [Harbor](https://github.com/z1joey/harbor) — drafts and validates `harbor.toml` configs from your repo layout, plans ports across registered projects, and catches schema mistakes before you add a folder in the app.

## Install

```bash
mkdir -p ~/.agents/skills
git clone https://github.com/z1joey/harbor-toml.git ~/.agents/skills/harbor-toml
```

In Cursor chat, attach or invoke the **harbor-toml** skill and ask e.g. *"接入 harbor，帮我写个 harbor 配置"* or *"Add `~/Projects/my-app` to Harbor — write harbor.toml"*.

## Validate without the app

```bash
python3 ~/.agents/skills/harbor-toml/scripts/validate_harbor_toml.py \
  path/to/harbor.toml \
  path/to/other-project/harbor.toml
```

Pass every registered project's config to catch static port overlaps. Exit `0` = OK; `1` = errors/overlaps; `2` = missing Python TOML library (`tomli` on Python &lt; 3.11).

## Contents

| Path | Purpose |
|------|---------|
| `SKILL.md` | Full workflow, schema, port-planning rules for the agent |
| `scripts/validate_harbor_toml.py` | Parser-aligned validator + cross-project overlap checks |

The skill writes `harbor.toml` on disk only — register the folder in Harbor via **Add Project…** after saving.
