#!/usr/bin/env python3
"""SessionStart hook：注入共享记忆上下文 + 记录会话基线（供 checkpoint 归属用）。

stdin: hook JSON（含 cwd 等）；stdout: additionalContext JSON。
任何失败都静默退出（exit 0），绝不阻塞 agent 启动。
用法: session_context_hook.py <agent_name>
"""
import hashlib
import json
import os
import sys
import urllib.request
from datetime import datetime, timezone

ROOT = os.path.dirname(os.path.dirname(os.path.realpath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "lib"))
from projectid import detect_project_id  # noqa: E402

AGENT = sys.argv[1] if len(sys.argv) > 1 else "cli"
CONFIG_PATH = os.environ.get("AGENT_MEMORY_CONFIG", os.path.join(ROOT, "config.json"))
STATE_DIR = os.path.join(ROOT, "state")


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
        body = json.dumps({"project_id": detect_project_id(cwd, timeout=5)[0]}).encode()
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
