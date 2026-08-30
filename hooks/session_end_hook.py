#!/usr/bin/env python3
"""SessionEnd hook：异步触发 memory checkpoint（detached，不阻塞会话退出）。

stdin: hook JSON（含 cwd / transcript_path）。
尽力从 transcript 提取最近的 assistant 文本作为会话产出，交给 LLM 提取；
任何失败都静默退出（exit 0），绝不影响会话关闭。
用法: session_end_hook.py <agent_name>
"""
import json
import os
import subprocess
import sys
import tempfile

AGENT = sys.argv[1] if len(sys.argv) > 1 else "cli"
MEMORY_CLI = os.path.expanduser("~/.local/bin/memory")
LOG = os.path.expanduser("~/agent_memory/logs/hooks.log")


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
        output_text = extract_last_assistant_text(transcript)
        tmpf = ""
        if output_text:
            fd, tmpf = tempfile.mkstemp(prefix="mem-out-", suffix=".txt")
            os.write(fd, output_text.encode())
            os.close(fd)
        cmd = [MEMORY_CLI, "checkpoint", "--repo", cwd, "--agent", AGENT]
        if tmpf:
            cmd += ["--output-file", tmpf]
        env = dict(os.environ, AGENT_NAME=AGENT)
        with open(LOG, "a") as logf:
            subprocess.Popen(
                cmd, stdout=logf, stderr=subprocess.STDOUT,
                stdin=subprocess.DEVNULL, start_new_session=True, env=env)
        # 临时产出文件由 checkpoint 读取后即无用；标记时间戳便于人工清理
    except Exception:
        pass
    sys.exit(0)


if __name__ == "__main__":
    main()
