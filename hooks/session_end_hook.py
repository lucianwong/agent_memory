#!/usr/bin/env python3
"""SessionEnd hook：异步触发 memory checkpoint（detached，不阻塞会话退出）。

- 读取 SessionStart 写入的会话基线（started_at），用于 diff 归属过滤；
- 扫描同目录其它 agent 的活跃基线 → 并行会话检测，传递给 checkpoint 做保守提取；
- 尽力从 transcript 提取最近 assistant 文本作为会话产出；
- 任何失败都静默退出（exit 0），绝不影响会话关闭。
用法: session_end_hook.py <agent_name>
"""
import hashlib
import json
import os
import subprocess
import sys
import tempfile
from datetime import datetime, timezone

AGENT = sys.argv[1] if len(sys.argv) > 1 else "cli"
MEMORY_CLI = os.path.expanduser("~/.local/bin/mem")
STATE_DIR = os.path.expanduser("~/agent_memory/state")
LOG = os.path.expanduser("~/agent_memory/logs/hooks.log")
FRESH_SECONDS = 12 * 3600  # 基线超过 12h 视为陈旧残留，不参与并行检测


def cwd_key(cwd: str) -> str:
    return hashlib.sha256(cwd.encode()).hexdigest()[:12]


def read_own_started_at(cwd: str) -> str | None:
    path = os.path.join(STATE_DIR, f"{AGENT}.{cwd_key(cwd)}.json")
    try:
        with open(path) as f:
            data = json.load(f)
        os.remove(path)
        return data.get("started_at")
    except (OSError, json.JSONDecodeError):
        return None


def detect_parallel(cwd: str) -> list[str]:
    """同 cwd、其它 agent、且基线仍然新鲜 → 并行会话。"""
    key = cwd_key(cwd)
    now = datetime.now(timezone.utc)
    found: dict[str, str] = {}
    try:
        names = os.listdir(STATE_DIR)
    except OSError:
        return []
    for name in names:
        if not name.endswith(f".{key}.json"):
            continue
        other, _, _ = name.partition(".")
        if not other or other == AGENT:
            continue
        try:
            with open(os.path.join(STATE_DIR, name)) as f:
                data = json.load(f)
            started = datetime.fromisoformat(data.get("started_at", ""))
            if (now - started).total_seconds() <= FRESH_SECONDS:
                found[data.get("agent", other)] = name
        except (OSError, json.JSONDecodeError, ValueError):
            continue
    return sorted(found)


def extract_last_assistant_text(path: str, max_chars: int = 4000,
                                max_items: int = 3) -> str:
    """尽力从 JSONL transcript 提取最近几条 assistant 文本（格式无关启发式）。"""
    if not path or not os.path.isfile(path):
        return ""
    try:
        with open(path, "rb") as f:
            f.seek(0, os.SEEK_END)
            size = f.tell()
            f.seek(max(0, size - 262144))
            blob = f.read().decode(errors="replace")
    except OSError:
        return ""

    def collect(o: object, found: list) -> None:
        if isinstance(o, dict):
            for k, v in o.items():
                if k == "text" and isinstance(v, str) and v.strip():
                    found.append(v.strip())
                else:
                    collect(v, found)
        elif isinstance(o, list):
            for v in o:
                collect(v, found)

    texts: list[str] = []
    for line in reversed(blob.splitlines()):
        if '"assistant"' not in line:
            continue
        try:
            obj = json.loads(line)
        except Exception:
            continue
        found: list[str] = []
        collect(obj, found)
        if found:
            texts.append("\n".join(found))
        if len(texts) >= max_items:
            break
    return ("\n---\n".join(reversed(texts)))[:max_chars]


def main() -> None:
    try:
        raw = sys.stdin.read() or "{}"
        data = json.loads(raw) if raw.strip().startswith("{") else {}
    except Exception:
        data = {}
    cwd = data.get("cwd") or os.getcwd()
    transcript = data.get("transcript_path") or ""

    try:
        started_at = read_own_started_at(cwd)
        parallel = detect_parallel(cwd)
        output_text = extract_last_assistant_text(transcript)
        tmpf = ""
        if output_text:
            fd, tmpf = tempfile.mkstemp(prefix="mem-out-", suffix=".txt")
            os.write(fd, output_text.encode())
            os.close(fd)
        cmd = [MEMORY_CLI, "cp", "--repo", cwd, "--agent", AGENT]
        if started_at:
            cmd += ["--started-at", started_at]
        if parallel:
            cmd += ["--parallel", ",".join(parallel)]
        if tmpf:
            cmd += ["--output-file", tmpf]
        env = dict(os.environ, AGENT_NAME=AGENT)
        with open(LOG, "a") as logf:
            subprocess.Popen(
                cmd, stdout=logf, stderr=subprocess.STDOUT,
                stdin=subprocess.DEVNULL, start_new_session=True, env=env)
    except Exception:
        pass
    sys.exit(0)


if __name__ == "__main__":
    main()
