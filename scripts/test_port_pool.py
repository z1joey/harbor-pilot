#!/usr/bin/env python3
"""Tests for Harbor 1.1.0 pool helpers, next_pool_port.py, and the validator."""

from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import textwrap
import unittest

SCRIPTS = os.path.dirname(os.path.abspath(__file__))
if SCRIPTS not in sys.path:
    sys.path.insert(0, SCRIPTS)

import harbor_pool  # noqa: E402
from harbor_pool import (  # noqa: E402
    PoolError,
    claimed_ports_from_data,
    next_free_ports,
    parse_lsof_listen_ports,
    parse_pool_data,
    port_in_pool,
)

NEXT_POOL = os.path.join(SCRIPTS, "next_pool_port.py")
VALIDATE = os.path.join(SCRIPTS, "validate_harbor_toml.py")


def write(path, text):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as fh:
        fh.write(textwrap.dedent(text).lstrip("\n"))
    return path


def run_script(script, args, **kwargs):
    env = os.environ.copy()
    env["PYTHONPATH"] = SCRIPTS + os.pathsep + env.get("PYTHONPATH", "")
    return subprocess.run(
        [sys.executable, script, *args],
        capture_output=True,
        text=True,
        env=env,
        **kwargs,
    )


SAMPLE_PROCESS = """
name = "{name}"

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


class ScriptTests(unittest.TestCase):
    def test_next_pool_port_cli(self):
        with tempfile.TemporaryDirectory() as tmp:
            pool = write(os.path.join(tmp, "port-pool.json"), """
            { "ranges": [{ "from": 8100, "to": 8199 }] }
            """)
            sibling = write(os.path.join(tmp, "other", "harbor.toml"), SAMPLE_PROCESS.format(name="other", port=8100))
            claim = write(os.path.join(tmp, "db", "harbor.toml"), """
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

    def test_next_pool_port_from_projects_json(self):
        with tempfile.TemporaryDirectory() as tmp:
            project = os.path.join(tmp, "shop")
            write(os.path.join(project, "harbor.toml"), SAMPLE_PROCESS.format(name="shop", port=8100))
            registry = write(os.path.join(tmp, "projects.json"), json.dumps([project]))
            pool = write(os.path.join(tmp, "port-pool.json"), """
            { "ranges": [{ "from": 8100, "to": 8199 }] }
            """)
            result = run_script(NEXT_POOL, ["--pool", pool, "--projects-json-path", registry])
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
            path = write(os.path.join(tmp, "harbor.toml"), """
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
            path = write(os.path.join(tmp, "harbor.toml"), """
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

    def test_validate_overlap_exits_1(self):
        with tempfile.TemporaryDirectory() as tmp:
            a = write(os.path.join(tmp, "a", "harbor.toml"), SAMPLE_PROCESS.format(name="a", port=8100))
            b = write(os.path.join(tmp, "b", "harbor.toml"), SAMPLE_PROCESS.format(name="b", port=8100))
            result = run_script(VALIDATE, [a, b])
            self.assertEqual(result.returncode, 1)
            self.assertIn("OVERLAP  port 8100", result.stdout)

    def test_validate_warns_out_of_pool_managed_server(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = write(os.path.join(tmp, "harbor.toml"), """
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
            path = write(os.path.join(tmp, "harbor.toml"), """
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
