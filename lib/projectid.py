"""project_id 推导（Bridge / CLI / hooks 共用，仅依赖标准库）。

git remote URL 归一化为 host/owner/repo；无 remote 时回退 local-<路径哈希>。
URL 内嵌凭证（user:token@）与端口一律剥离，避免凭证进入记忆 metadata，
也保证同一仓库在不同机器/协议下得到同一个 project_id。
"""
import hashlib
import re
import subprocess

_SCHEME = re.compile(r"^[a-z][a-z0-9+.-]*://", re.I)


def normalize_remote(url: str) -> str:
    """git remote URL -> 标准化 repo id，如 github.com/yves/my-project。"""
    url = url.strip().split("#", 1)[0]
    m = _SCHEME.match(url)
    if m:
        host, _, path = url[m.end():].partition("/")
        host = host.rsplit("@", 1)[-1]  # 剥离 userinfo（含 token）
        host = host.split(":", 1)[0]    # 剥离端口
        url = host + "/" + path
    else:
        head = url.split("/", 1)[0]
        if ":" in head:  # scp 风格 [user@]host:path
            host, _, path = url.partition(":")
            url = host.rsplit("@", 1)[-1] + "/" + path
    if url.endswith(".git"):
        url = url[:-4]
    return url.strip("/")


def detect_project_id(path: str, timeout: int = 10) -> tuple[str, str]:
    """返回 (project_id, origin)。无 remote 或非 git 目录时回退 local-<pathhash>。"""
    origin = ""
    try:
        r = subprocess.run(["git", "-C", path, "remote", "get-url", "origin"],
                           capture_output=True, text=True, timeout=timeout)
        if r.returncode == 0:
            origin = r.stdout.strip()
    except (subprocess.SubprocessError, OSError):
        origin = ""
    if origin:
        return normalize_remote(origin), origin
    base = path
    try:
        r = subprocess.run(["git", "-C", path, "rev-parse", "--show-toplevel"],
                           capture_output=True, text=True, timeout=timeout)
        if r.returncode == 0 and r.stdout.strip():
            base = r.stdout.strip()
    except (subprocess.SubprocessError, OSError):
        pass
    return "local-" + hashlib.sha256(base.encode()).hexdigest()[:12], ""
