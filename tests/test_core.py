"""核心逻辑单测：python3 -m unittest discover tests（server 部分需要 fastapi）。"""
import importlib
import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "lib"))
sys.path.insert(0, str(ROOT / "bridge"))

from projectid import detect_project_id, normalize_remote  # noqa: E402


class NormalizeRemoteTest(unittest.TestCase):
    def test_equivalent_forms(self):
        for url in ("git@github.com:a/b.git", "https://github.com/a/b",
                    "https://github.com/a/b.git", "ssh://git@github.com/a/b.git",
                    "ssh://git@github.com:22/a/b.git", "git://github.com/a/b",
                    "https://github.com/a/b.git#main"):
            self.assertEqual(normalize_remote(url), "github.com/a/b", url)

    def test_strips_embedded_credentials(self):
        for url in ("https://x-access-token:ghp_secret123@github.com/a/b.git",
                    "https://user:pass@gitlab.example.com:8443/a/b"):
            pid = normalize_remote(url)
            self.assertNotIn("ghp_secret123", pid)
            self.assertNotIn("pass", pid)
            self.assertTrue(pid.endswith("/a/b"), pid)

    def test_local_fallback(self):
        with tempfile.TemporaryDirectory() as d:
            subprocess.run(["git", "init", "-q", d], check=True)
            pid, origin = detect_project_id(d)
            self.assertTrue(pid.startswith("local-"))
            self.assertEqual(origin, "")


def _load_server(cfg: dict):
    tmp = tempfile.mkdtemp()
    path = os.path.join(tmp, "config.json")
    with open(path, "w") as f:
        json.dump(cfg, f)
    os.environ["AGENT_MEMORY_CONFIG"] = path
    sys.modules.pop("server", None)
    try:
        return importlib.import_module("server"), tmp
    finally:
        os.environ.pop("AGENT_MEMORY_CONFIG", None)


try:
    import fastapi  # noqa: F401
    HAVE_FASTAPI = True
except ImportError:
    HAVE_FASTAPI = False


@unittest.skipUnless(HAVE_FASTAPI, "fastapi not installed")
class ServerTest(unittest.TestCase):
    def setUp(self):
        cfg = json.loads((ROOT / "config.example.json").read_text())
        self.server, self.tmp = _load_server(cfg)

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_example_config_loads(self):
        self.assertEqual(self.server.DS["model"], "deepseek-chat")

    def test_legacy_deepseek_key_migrates(self):
        cfg = json.loads((ROOT / "config.example.json").read_text())
        cfg["deepseek"] = cfg.pop("llm")
        server, tmp = _load_server(cfg)
        self.addCleanup(shutil.rmtree, tmp, True)
        self.assertEqual(server.CONFIG["llm"]["model"], "deepseek-chat")

    def test_redact(self):
        out = self.server.redact("key sk-abcdefghijklmnop1234 and "
                                 "https://u:p4ss@host/x api_key=zzzzzzzz")
        for secret in ("sk-abcdefghijklmnop1234", "p4ss", "zzzzzzzz"):
            self.assertNotIn(secret, out)

    def test_context_uses_three_queries(self):
        calls = []

        class FakeClient:
            def query_all(self, filters):
                calls.append(filters)
                return [{"id": str(len(calls)), "text": "t", "created_at": "",
                         "metadata": {"type": filters.get("type", "decision")}}]

        self.server.client = FakeClient()
        auth = "Bearer " + self.server.TOKEN
        out = self.server.context(self.server.ContextReq(project_id="p"), auth)
        self.assertEqual(len(calls), 3)
        self.assertIn("# Recent decisions", out["markdown"])
        self.assertIn("# User preferences", out["markdown"])

    def test_checkpoint_redacts_prompt_and_sees_commits(self):
        repo = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, repo, True)
        git = ["git", "-C", repo, "-c", "user.name=t", "-c", "user.email=t@t"]
        subprocess.run(["git", "init", "-q", repo], check=True)
        subprocess.run(git + ["commit", "-q", "--allow-empty", "-m", "base"], check=True)
        Path(repo, "a.txt").write_text("token=supersecretvalue\n")
        subprocess.run(git + ["add", "."], check=True)
        subprocess.run(git + ["commit", "-q", "-m", "add a"], check=True)

        prompts = []
        self.server._llm = lambda p: prompts.append(p) or "{}"
        auth = "Bearer " + self.server.TOKEN
        self.server.checkpoint(self.server.CheckpointReq(
            repo_path=repo, started_at="2000-01-01T00:00:00+00:00"), auth)
        self.assertEqual(len(prompts), 1)
        self.assertIn("add a", prompts[0])
        self.assertNotIn("supersecretvalue", prompts[0])


if __name__ == "__main__":
    unittest.main()
