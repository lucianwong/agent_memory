#!/usr/bin/env python3
"""SessionStart hook：注入共享记忆上下文 + 记录会话基线（供 checkpoint 归属用）。

stdin: hook JSON（含 cwd 等）；stdout: additionalContext JSON。
任何失败都静默退出（exit 0），绝不阻塞 agent 启动。
用法: session_context_hook.py <agent_name>
"""
import hashlib
import json
import os
import re
import subprocess
import sys
import urllib.request
from datetime import datetime, timezone

AGENT = sys.argv[1] if len(sys.argv) > 1 else "cli"
CONFIG_PATH = os.environ.get(
    "AGENT_MEMORY_CONFIG",
    os.path.expanduser("~/agent_memory/config.json"))
STATE_DIR = os.path.expanduser("~/agent_memory/state")


def project_id(path: str) -> str:
    try:
        r = subprocess.run(["git", "-C", path, "remote", "get-url", "origin"],
                           capture_output=True, text=True, timeout=5, check=True)
        url = r.stdout.strip()
        if url.startswith("git@"):
            url = url.split("git@", 1)[1]
        url = re.sub(r"^(ssh|https?|git)://", "", url)
        if "://" not in url and ":" in url.split("/", 1)[0]:
            url = url.replace(":", "/", 1)
        if url.endswith(".git"):
            url = url[:-4]
        return url.strip("/")
    except Exception:
        try:
            r = subprocess.run(["git", "-C", path, "rev-parse", "--show-toplevel"],
                               capture_output=True, text=True, timeout=5)
            base = r.stdout.strip() if r.returncode == 0 else path
        except Exception:
            base = path
        return "local-" + hashlib.sha256(base.encode()).hexdigest()[:12]


def write_state(cwd: str) -> None:
    """记录会话基线：checkpoint 用 started_at 做 diff 归属过滤。"""
    try:
        os.makedirs(STATE_DIR, exist_ok=True)
        key = hashlib.sha256(cwd.encode()).hexdigest()[:12]
        path = os.path.join(STATE_DIR, f"{AGENT}.{key}.json")
        tmp = path + ".tmp"
        with open(tmp, "w") as f:
            json.dump({"agent": AGENT, "cwd": cwd,
                       "started_at": datetime.now(timezone.utc).isoformat(),
                       "pid": os.getpid()}, f)
        os.replace(tmp, path)
    except Exception:
        pass  # 状态文件失败不影响注入与启动


def main() -> None:
    try:
        raw = sys.stdin.read() or "{}"
        data = json.loads(raw) if raw.strip().startswith("{") else {}
    except Exception:
        data = {}
    cwd = data.get("cwd") or os.getcwd()
    write_state(cwd)

    try:
        cfg = json.load(open(CONFIG_PATH))
        body = json.dumps({"project_id": project_id(cwd)}).encode()
        req = urllib.request.Request(
            cfg["bridge_url"].rstrip("/") + "/context", data=body, method="POST",
            headers={"Content-Type": "application/json",
                     "Authorization": "Bearer " + cfg["bridge_token"]})
        opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
        with opener.open(req, timeout=8) as resp:
            out = json.load(resp)
        md, total = out.get("markdown", ""), out.get("total", 0)
        if md and total > 0:
            tail = ("\n\n(以上为共享记忆系统自动注入；工作中有长期有效信息用 "
                    "`mem add` 写入，会话结束系统自动 checkpoint)")
            print(json.dumps({"hookSpecificOutput": {
                "hookEventName": "SessionStart",
                "additionalContext": md + tail}}, ensure_ascii=False))
    except Exception:
        pass
    sys.exit(0)


if __name__ == "__main__":
    main()
