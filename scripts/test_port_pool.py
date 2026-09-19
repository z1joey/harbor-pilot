#!/usr/bin/env python3
"""Tests for Harbor 1.3.0 central-store helpers, the CLIs, and the validator."""

from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import textwrap
import unittest
import unittest.mock

SCRIPTS = os.path.dirname(os.path.abspath(__file__))
if SCRIPTS not in sys.path:
    sys.path.insert(0, SCRIPTS)

import harbor_pool  # noqa: E402
from harbor_pool import (  # noqa: E402
    PoolError,
    claimed_ports_from_data,
    migrate_legacy_configs,
    migrate_legacy_stores,
    next_free_ports,
    parse_lsof_listen_ports,
    parse_pool_data,
    port_in_pool,
    register_project,
    unregister_project,
)

NEXT_POOL = os.path.join(SCRIPTS, "next_pool_port.py")
VALIDATE = os.path.join(SCRIPTS, "validate_harbor_toml.py")
REGISTER = os.path.join(SCRIPTS, "register_project.py")
UNREGISTER = os.path.join(SCRIPTS, "unregister_project.py")


def write(path, text):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as fh:
        fh.write(textwrap.dedent(text).lstrip("\n"))
    return path


def run_script(script, args, stdin=None, **kwargs):
    env = os.environ.copy()
    env["PYTHONPATH"] = SCRIPTS + os.pathsep + env.get("PYTHONPATH", "")
    return subprocess.run(
        [sys.executable, script, *args],
        capture_output=True,
        text=True,
        input=stdin,
        env=env,
        **kwargs,
    )


SAMPLE_PROCESS = """
root = "{root}"

[[process]]
name = "web"
command = "npm run dev -- --port $PORT"
port = {port}
ready_url = "http://127.0.0.1:${{port}}/"
"""


class ParsePoolTests(unittest.TestCase):
    def test_default_contract_shape(self):
        ranges = parse_pool_data({"ranges": [{"from": 8100, "to": 8199}]})
        self.assertEqual(ranges, [(8100, 8199)])

    def test_rejects_inverted_and_overlapping(self):
        with self.assertRaises(PoolError):
            parse_pool_data({"ranges": [{"from": 8200, "to": 8100}]})
        with self.assertRaises(PoolError):
            parse_pool_data({
                "ranges": [{"from": 8100, "to": 8150}, {"from": 8150, "to": 8199}],
            })

    def test_rejects_out_of_range(self):
        with self.assertRaises(PoolError):
            parse_pool_data({"ranges": [{"from": 0, "to": 10}]})

    def test_adjacent_ranges_ok(self):
        ranges = parse_pool_data({
            "ranges": [{"from": 8100, "to": 8149}, {"from": 8150, "to": 8199}],
        })
        self.assertEqual(ranges, [(8100, 8149), (8150, 8199)])

    def test_missing_file_defaults(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "nope.json")
            self.assertEqual(harbor_pool.load_port_pool(path, missing_ok=True), [(8100, 8199)])


class NextFreeTests(unittest.TestCase):
    def test_skips_taken_and_claims(self):
        data = {
            "process": [{"name": "web", "command": "x", "port": 8100}],
            "port_claim": [{"port": 8101}],
        }
        taken = claimed_ports_from_data(data)
        self.assertEqual(taken, {8100, 8101})
        self.assertEqual(next_free_ports([(8100, 8199)], taken), [8102])

    def test_exhausted(self):
        with self.assertRaises(PoolError):
            next_free_ports([(8100, 8101)], {8100, 8101})

    def test_count(self):
        self.assertEqual(next_free_ports([(8100, 8199)], {8100}, count=2), [8101, 8102])

    def test_port_in_pool(self):
        ranges = [(8100, 8199)]
        self.assertTrue(port_in_pool(8100, ranges))
        self.assertFalse(port_in_pool(5173, ranges))

    def test_lsof_parse(self):
        text = "node 1 me  3u  IPv4  TCP *:8100 (LISTEN)\nnginx 2 me 4u TCP 127.0.0.1:5432 (LISTEN)\n"
        self.assertEqual(parse_lsof_listen_ports(text), {8100, 5432})


class HarborHomeTests(unittest.TestCase):
    """The ~/.harbor contract: paths, legacy fallback, migration, registration."""

    def test_default_paths_point_at_harbor_home(self):
        self.assertTrue(harbor_pool.HARBOR_DIR.endswith("/.harbor"))
        self.assertEqual(harbor_pool.DEFAULT_POOL_PATH, os.path.join(harbor_pool.HARBOR_DIR, "port-pool.json"))
        self.assertEqual(harbor_pool.DEFAULT_PROJECTS_JSON, os.path.join(harbor_pool.HARBOR_DIR, "projects.json"))
        self.assertEqual(harbor_pool.PROJECTS_DIR, os.path.join(harbor_pool.HARBOR_DIR, "projects"))

    def test_fallback_to_legacy_while_only_legacy_exists(self):
        with tempfile.TemporaryDirectory() as tmp:
            new_dir = os.path.join(tmp, "new")
            legacy_dir = os.path.join(tmp, "legacy")
            os.makedirs(legacy_dir)
            write(os.path.join(legacy_dir, "port-pool.json"), '{"ranges": [{"from": 8200, "to": 8299}]}')
            write(os.path.join(legacy_dir, "projects.json"), "[]")
            with unittest.mock.patch.object(harbor_pool, "HARBOR_DIR", new_dir), \
                 unittest.mock.patch.object(harbor_pool, "DEFAULT_POOL_PATH", os.path.join(new_dir, "port-pool.json")), \
                 unittest.mock.patch.object(harbor_pool, "DEFAULT_PROJECTS_JSON", os.path.join(new_dir, "projects.json")), \
                 unittest.mock.patch.object(harbor_pool, "LEGACY_DIR", legacy_dir), \
                 unittest.mock.patch.object(harbor_pool, "LEGACY_POOL_PATH", os.path.join(legacy_dir, "port-pool.json")), \
                 unittest.mock.patch.object(harbor_pool, "LEGACY_PROJECTS_JSON", os.path.join(legacy_dir, "projects.json")):
                self.assertEqual(harbor_pool.default_pool_path(), os.path.join(legacy_dir, "port-pool.json"))
                self.assertEqual(harbor_pool.default_projects_path(), os.path.join(legacy_dir, "projects.json"))
                os.makedirs(new_dir)
                write(os.path.join(new_dir, "projects.json"), "[]")
                self.assertEqual(harbor_pool.default_projects_path(), os.path.join(new_dir, "projects.json"))

    def test_migrate_moves_stores_and_cleans_legacy(self):
        with tempfile.TemporaryDirectory() as tmp:
            harbor_dir = os.path.join(tmp, ".harbor")
            legacy_dir = os.path.join(tmp, "legacy")
            os.makedirs(legacy_dir)
            write(os.path.join(legacy_dir, "projects.json"), '["/tmp/a"]')
            write(os.path.join(legacy_dir, "port-pool.json"), "{}")
            write(os.path.join(legacy_dir, "projects.json.lock"), "")
            migrate_legacy_stores(harbor_dir=harbor_dir, legacy_dir=legacy_dir)
            self.assertTrue(os.path.isfile(os.path.join(harbor_dir, "projects.json")))
            self.assertTrue(os.path.isfile(os.path.join(harbor_dir, "port-pool.json")))
            self.assertFalse(os.path.isdir(legacy_dir))

    def test_migrate_keeps_existing_harbor_files(self):
        with tempfile.TemporaryDirectory() as tmp:
            harbor_dir = os.path.join(tmp, ".harbor")
            legacy_dir = os.path.join(tmp, "legacy")
            os.makedirs(harbor_dir)
            os.makedirs(legacy_dir)
            write(os.path.join(harbor_dir, "projects.json"), '["/fresh"]')
            write(os.path.join(legacy_dir, "projects.json"), '["/old"]')
            migrate_legacy_stores(harbor_dir=harbor_dir, legacy_dir=legacy_dir)
            with open(os.path.join(harbor_dir, "projects.json"), encoding="utf-8") as fh:
                self.assertEqual(json.load(fh), ["/fresh"])


class CentralConfigMigrationTests(unittest.TestCase):
    """Root harbor.toml files are imported into ~/.harbor/projects/ once."""

    def test_imports_root_configs_with_root_key_injected(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = os.path.join(tmp, "steward")
            write(os.path.join(root, "harbor.toml"), SAMPLE_PROCESS.format(root="/ignored", port=8100))
            registry = write(os.path.join(tmp, "projects.json"), json.dumps([root]))
            projects_dir = os.path.join(tmp, "central")

            migrate_legacy_configs(projects_dir=projects_dir, registry_path=registry)

            installed = os.path.join(projects_dir, "steward.toml")
            self.assertTrue(os.path.isfile(installed))
            with open(installed, encoding="utf-8") as fh:
                content = fh.read()
            self.assertIn(f'root = "{root}"', content)
            self.assertIn('name = "web"', content)
            # Filename fell back to the root folder name (no `name` key in the TOML).
            self.assertEqual(os.path.basename(installed), "steward.toml")
            # The root-side file is never modified or deleted.
            self.assertTrue(os.path.isfile(os.path.join(root, "harbor.toml")))
            self.assertTrue(os.path.isfile(registry))

    def test_uses_dot_harbor_toml_flavor_and_folder_name_fallback(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = os.path.join(tmp, "wordlist-fullstack")
            write(os.path.join(root, ".harbor.toml"), """
            [[process]]
            name = "app"
            command = "docker compose up --build"
            """)
            registry = write(os.path.join(tmp, "projects.json"), json.dumps([root]))
            projects_dir = os.path.join(tmp, "central")

            migrate_legacy_configs(projects_dir=projects_dir, registry_path=registry)

            installed = os.path.join(projects_dir, "wordlist-fullstack.toml")
            self.assertTrue(os.path.isfile(installed))
            with open(installed, encoding="utf-8") as fh:
                self.assertIn(f'root = "{root}"', fh.read())

    def test_existing_central_files_win_and_import_is_idempotent(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = os.path.join(tmp, "shop")
            write(os.path.join(root, "harbor.toml"), SAMPLE_PROCESS.format(root="/ignored", port=8100))
            registry = write(os.path.join(tmp, "projects.json"), json.dumps([root]))
            projects_dir = os.path.join(tmp, "central")
            write(os.path.join(projects_dir, "already.toml"), f'root = "{root}"\nname = "already-here"\n')

            migrate_legacy_configs(projects_dir=projects_dir, registry_path=registry)
            self.assertEqual(os.listdir(projects_dir), ["already.toml"])
            migrate_legacy_configs(projects_dir=projects_dir, registry_path=registry)
            self.assertEqual(os.listdir(projects_dir), ["already.toml"])

    def test_root_without_config_is_skipped(self):
        with tempfile.TemporaryDirectory() as tmp:
            empty = os.path.join(tmp, "empty")
            os.makedirs(empty)
            registry = write(os.path.join(tmp, "projects.json"), json.dumps([empty]))
            projects_dir = os.path.join(tmp, "central")

            migrate_legacy_configs(projects_dir=projects_dir, registry_path=registry)

            self.assertEqual(os.listdir(projects_dir), [])


class RegisterProjectTests(unittest.TestCase):
    """register_project installs the central config; no root harbor.toml needed."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.tmp = self._tmp.name
        self.projects_dir = os.path.join(self.tmp, "central")
        self.root = os.path.join(self.tmp, "shop")
        os.makedirs(self.root)
        self.draft = write(os.path.join(self.tmp, "draft.toml"),
                           SAMPLE_PROCESS.format(root="/ignored", port=8100))

    def central(self):
        return os.listdir(self.projects_dir) if os.path.isdir(self.projects_dir) else []

    def test_register_from_config_file(self):
        result = register_project(self.root, config_path=self.draft,
                                  projects_dir=self.projects_dir)
        self.assertEqual(result, "registered")
        self.assertEqual(self.central(), ["shop.toml"])
        with open(os.path.join(self.projects_dir, "shop.toml"), encoding="utf-8") as fh:
            content = fh.read()
        self.assertIn(f'root = "{self.root}"', content)
        self.assertNotIn("/ignored", content)

    def test_register_from_stdin_without_root_config(self):
        stdin_text = SAMPLE_PROCESS.format(root="/ignored", port=8100)
        with unittest.mock.patch.object(sys, "stdin", io_string(stdin_text)):
            result = register_project(self.root, projects_dir=self.projects_dir)
        self.assertEqual(result, "registered")
        self.assertEqual(self.central(), ["shop.toml"])

    def test_re_registering_is_updated_then_unchanged(self):
        first = register_project(self.root, config_path=self.draft,
                                 projects_dir=self.projects_dir)
        self.assertEqual(first, "registered")
        same = register_project(self.root, config_path=self.draft,
                                projects_dir=self.projects_dir)
        self.assertEqual(same, "unchanged")
        self.assertEqual(self.central(), ["shop.toml"])

        changed = write(os.path.join(self.tmp, "draft2.toml"),
                        SAMPLE_PROCESS.format(root="/ignored", port=8101))
        second = register_project(self.root, config_path=changed,
                                  projects_dir=self.projects_dir)
        self.assertEqual(second, "updated")
        self.assertEqual(self.central(), ["shop.toml"], "update replaces the same file")
        with open(os.path.join(self.projects_dir, "shop.toml"), encoding="utf-8") as fh:
            self.assertIn("port = 8101", fh.read())

    def test_register_rejects_bad_structure_and_port_clashes(self):
        broken = write(os.path.join(self.tmp, "broken.toml"), "name = [oops\n")
        with self.assertRaises(PoolError):
            register_project(self.root, config_path=broken, projects_dir=self.projects_dir)
        self.assertEqual(self.central(), [])

        register_project(self.root, config_path=self.draft, projects_dir=self.projects_dir)
        clash = write(os.path.join(self.tmp, "clash.toml"),
                      SAMPLE_PROCESS.format(root="/ignored", port=8100))
        other = os.path.join(self.tmp, "other")
        os.makedirs(other)
        with self.assertRaises(PoolError):
            register_project(other, config_path=clash, projects_dir=self.projects_dir)

    def test_register_rejects_traversal_relative_and_missing_root(self):
        traversal = os.path.join(self.tmp, "a", os.pardir, "b")
        with self.assertRaises(PoolError):
            register_project(traversal, config_path=self.draft,
                             projects_dir=self.projects_dir)
        with self.assertRaises(PoolError):
            register_project("relative/path", config_path=self.draft,
                             projects_dir=self.projects_dir)
        with self.assertRaises(PoolError):
            register_project(os.path.join(self.tmp, "does-not-exist"),
                             config_path=self.draft, projects_dir=self.projects_dir)
        with self.assertRaises(PoolError):
            register_project(self.root, projects_dir=self.projects_dir)  # no stdin

    def test_register_syncs_projects_json_mirror(self):
        mirror = os.path.join(self.tmp, "projects.json")
        register_project(self.root, config_path=self.draft,
                         projects_dir=self.projects_dir, projects_path=mirror)
        with open(mirror, encoding="utf-8") as fh:
            self.assertEqual(json.load(fh), [self.root])

        unregister_project(self.root, projects_dir=self.projects_dir, projects_path=mirror)
        with open(mirror, encoding="utf-8") as fh:
            self.assertEqual(json.load(fh), [])

    def test_unregister_removes_central_config(self):
        register_project(self.root, config_path=self.draft, projects_dir=self.projects_dir)
        self.assertEqual(unregister_project(self.root, projects_dir=self.projects_dir),
                         "unregistered")
        self.assertEqual(self.central(), [])
        self.assertEqual(unregister_project(self.root, projects_dir=self.projects_dir),
                         "not-registered")

    def test_register_cli(self):
        mirror = os.path.join(self.tmp, "projects.json")
        result = run_script(REGISTER, [self.root, "--config", self.draft,
                                       "--projects-dir", self.projects_dir,
                                       "--projects-json-path", mirror])
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("REGISTERED", result.stdout)
        self.assertTrue(os.path.isfile(os.path.join(self.projects_dir, "shop.toml")))

        again = run_script(REGISTER, [self.root, "--config", self.draft,
                                      "--projects-dir", self.projects_dir,
                                      "--projects-json-path", mirror])
        self.assertEqual(again.returncode, 0, again.stderr)
        self.assertIn("UNCHANGED", again.stdout)

    def test_register_cli_stdin(self):
        result = run_script(REGISTER, [self.root, "--projects-dir", self.projects_dir],
                            stdin=SAMPLE_PROCESS.format(root="/ignored", port=8100))
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("REGISTERED", result.stdout)

    def test_unregister_cli(self):
        run_script(REGISTER, [self.root, "--config", self.draft,
                              "--projects-dir", self.projects_dir])
        result = run_script(UNREGISTER, [self.root, "--projects-dir", self.projects_dir])
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("UNREGISTERED", result.stdout)
        self.assertEqual(self.central(), [])


class io_string:
    """Minimal stdin stand-in exposing isatty() = False and read()."""

    def __init__(self, text):
        self._text = text

    def isatty(self):
        return False

    def read(self):
        return self._text


class ScriptTests(unittest.TestCase):
    def test_next_pool_port_cli(self):
        with tempfile.TemporaryDirectory() as tmp:
            pool = write(os.path.join(tmp, "port-pool.json"), """
            { "ranges": [{ "from": 8100, "to": 8199 }] }
            """)
            sibling = write(os.path.join(tmp, "other.toml"), SAMPLE_PROCESS.format(root="/o", port=8100))
            claim = write(os.path.join(tmp, "db.toml"), """
            root = "/db"
            name = "db"
            [[process]]
            name = "deps"
            command = "docker compose up postgres"
            [[port_claim]]
            port = 8101
            """)
            result = run_script(NEXT_POOL, ["--pool", pool, "--count", "2", sibling, claim])
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual(result.stdout.strip().splitlines(), ["8102", "8103"])

    def test_next_pool_port_from_central_store(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = os.path.join(tmp, "shop")
            os.makedirs(root)
            projects_dir = os.path.join(tmp, "central")
            write(os.path.join(projects_dir, "shop.toml"),
                  SAMPLE_PROCESS.format(root=root, port=8100))
            pool = write(os.path.join(tmp, "port-pool.json"), """
            { "ranges": [{ "from": 8100, "to": 8199 }] }
            """)
            result = run_script(NEXT_POOL, ["--pool", pool, "--projects-dir", projects_dir])
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual(result.stdout.strip(), "8101")

    def test_next_pool_port_missing_pool_defaults(self):
        with tempfile.TemporaryDirectory() as tmp:
            missing = os.path.join(tmp, "absent.json")
            result = run_script(NEXT_POOL, ["--pool", missing])
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual(result.stdout.strip(), "8100")

    def test_validate_rejects_auto(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = write(os.path.join(tmp, "draft.toml"), """
            name = "bad"
            [[process]]
            name = "web"
            command = "npm run dev -- --port $PORT"
            port = "auto"
            """)
            result = run_script(VALIDATE, [path])
            self.assertEqual(result.returncode, 1)
            self.assertIn('port = "auto" is not allowed', result.stdout)

    def test_validate_allows_port_env_and_dollar_port(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = write(os.path.join(tmp, "draft.toml"), """
            name = "ok"
            [[process]]
            name = "api"
            command = "uv run uvicorn app.main:app --reload --port $APP_PORT"
            port = 8100
            port_env = "APP_PORT"
            ready_url = "http://127.0.0.1:${port}/health"
            """)
            pool = write(os.path.join(tmp, "port-pool.json"), """
            { "ranges": [{ "from": 8100, "to": 8199 }] }
            """)
            result = run_script(VALIDATE, ["--pool", pool, path])
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
            self.assertIn("OK", result.stdout)
            self.assertNotIn("WARN", result.stdout)

    def test_validate_flags_legacy_root_harbor_toml(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = write(os.path.join(tmp, "harbor.toml"), """
            name = "legacy"
            [[process]]
            name = "api"
            command = "run"
            port = 8100
            """)
            result = run_script(VALIDATE, [path])
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
            self.assertIn("no longer read by Harbor 1.3+", result.stdout)

    def test_validate_overlap_exits_1(self):
        with tempfile.TemporaryDirectory() as tmp:
            a = write(os.path.join(tmp, "a.toml"), SAMPLE_PROCESS.format(root="/a", port=8100))
            b = write(os.path.join(tmp, "b.toml"), SAMPLE_PROCESS.format(root="/b", port=8100))
            result = run_script(VALIDATE, [a, b])
            self.assertEqual(result.returncode, 1)
            self.assertIn("OVERLAP  port 8100", result.stdout)

    def test_validate_warns_out_of_pool_managed_server(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = write(os.path.join(tmp, "draft.toml"), """
            name = "web"
            [[process]]
            name = "web"
            command = "npm run dev -- --port $PORT"
            port = 5173
            """)
            pool = write(os.path.join(tmp, "port-pool.json"), """
            { "ranges": [{ "from": 8100, "to": 8199 }] }
            """)
            result = run_script(VALIDATE, ["--pool", pool, path])
            self.assertEqual(result.returncode, 0, result.stdout)
            self.assertIn("outside the Harbor pool", result.stdout)

    def test_validate_hardcoded_out_of_pool_is_ok(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = write(os.path.join(tmp, "draft.toml"), """
            name = "shop"
            [[process]]
            name = "api"
            command = "uv run uvicorn app.main:app --reload --port 8001"
            port = 8001
            ready_url = "http://127.0.0.1:8001/health"
            """)
            pool = write(os.path.join(tmp, "port-pool.json"), """
            { "ranges": [{ "from": 8100, "to": 8199 }] }
            """)
            result = run_script(VALIDATE, ["--pool", pool, path])
            self.assertEqual(result.returncode, 0, result.stdout)
            self.assertNotIn("outside the Harbor pool", result.stdout)

    def test_example_fullstack_validates(self):
        example = os.path.join(os.path.dirname(SCRIPTS), "examples", "fullstack.toml")
        with tempfile.TemporaryDirectory() as tmp:
            pool = write(os.path.join(tmp, "port-pool.json"), """
            { "ranges": [{ "from": 8100, "to": 8199 }] }
            """)
            result = run_script(VALIDATE, ["--pool", pool, example])
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
            self.assertIn("OK", result.stdout)
            self.assertNotIn("outside the Harbor pool", result.stdout)


if __name__ == "__main__":
    unittest.main()
