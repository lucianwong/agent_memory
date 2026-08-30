"""agent-memory Bridge：连接本地 CLI/Agent 与 oma 自部署 Mem0 的集成层（单文件实现）。

职责：Bearer 鉴权、凭证脱敏、project_id 归一、上下文组装、checkpoint LLM 提取。
无状态（除 config.json），全部记忆存 Mem0；Tailnet/LLM 调用强制绕过系统代理。
"""
import hashlib
import hmac
import json
import os
import re
import subprocess
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

from fastapi import FastAPI, Header, HTTPException
from fastapi.responses import FileResponse
from pydantic import BaseModel, Field

CONFIG_PATH = os.environ.get(
    "AGENT_MEMORY_CONFIG",
    str(Path.home() / "agent_memory" / "config.json"))
PROMPT_PATH = Path(__file__).resolve().parent.parent / "prompts" / "checkpoint.md"


def _load_config() -> dict:
    path = Path(CONFIG_PATH)
    try:
        with open(path) as f:
            cfg = json.load(f)
    except FileNotFoundError as e:
        raise RuntimeError(f"config not found: {path}") from e
    except json.JSONDecodeError as e:
        raise RuntimeError(f"invalid JSON in {path}") from e
    for key in ("bridge_token", "mem0_base_url", "mem0_api_key", "user_id", "deepseek"):
        if key not in cfg:
            raise RuntimeError(f"config missing key: {key}")
    try:
        cfg["dedup_threshold"] = float(cfg.get("dedup_threshold", 0.92))
    except (TypeError, ValueError) as e:
        raise RuntimeError("dedup_threshold must be a number") from e
    if not isinstance(cfg.get("context_limits", {}), dict):
        raise RuntimeError("context_limits must be an object")
    return cfg


def _save_config(cfg: dict) -> None:
    path = Path(CONFIG_PATH)
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(json.dumps(cfg, ensure_ascii=False, indent=2) + "\n")
    os.chmod(tmp, 0o600)
    os.replace(tmp, path)


def _migrate_config(cfg: dict) -> dict:
    """补齐新增配置键（llm / agents），保证旧版 config.json 平滑升级。"""
    changed = False
    if "llm" not in cfg and "deepseek" in cfg:
        cfg["llm"] = dict(cfg["deepseek"])
        changed = True
    if "agents" not in cfg:
        cfg["agents"] = {
            "codex": {"enabled": True, "hook_type": "codex"},
            "kimi": {"enabled": True, "hook_type": "kimi"},
            "pi": {"enabled": True, "hook_type": "none"},
            "zcode": {"enabled": True, "hook_type": "none"},
        }
        changed = True
    if changed:
        _save_config(cfg)
    return cfg


CONFIG = _migrate_config(_load_config())
TOKEN = CONFIG["bridge_token"]
LIMITS = CONFIG.get("context_limits", {
    "profile": 5, "project": 10, "decision": 10, "task": 5, "incident": 5})
DEDUP_THRESHOLD = CONFIG["dedup_threshold"]
DS = CONFIG.get("llm") or CONFIG["deepseek"]

NO_PROXY_OPENER = urllib.request.build_opener(urllib.request.ProxyHandler({}))


# ---------------------------------------------------------------------------
# Mem0 Self-Hosted REST 客户端
# ---------------------------------------------------------------------------
class Mem0Error(Exception):
    pass


class Mem0Client:
    def __init__(self, base_url: str, api_key: str, user_id: str, timeout: int = 30):
        self.base_url = base_url.rstrip("/")
        self.api_key = api_key
        self.user_id = user_id
        self.timeout = timeout

    def _request(self, method: str, path: str, body: dict | None = None) -> dict | list:
        data = json.dumps(body).encode() if body is not None else None
        req = urllib.request.Request(
            self.base_url + path, data=data, method=method,
            headers={"Content-Type": "application/json", "X-API-Key": self.api_key})
        try:
            with NO_PROXY_OPENER.open(req, timeout=self.timeout) as resp:
                return json.load(resp)
        except urllib.error.HTTPError as e:
            detail = e.read().decode(errors="replace")[:300]
            raise Mem0Error(f"mem0 {method} {path} -> HTTP {e.code}: {detail}") from e
        except (urllib.error.URLError, TimeoutError, OSError) as e:
            raise Mem0Error(f"mem0 {method} {path} unreachable: {e}") from e

    def add(self, content: str, metadata: dict) -> list[str]:
        """infer:false 原样写入，返回新记忆 id 列表。"""
        body = {
            "messages": [{"role": "user", "content": content}],
            "user_id": self.user_id,
            "metadata": metadata,
            "infer": False,
        }
        result = self._request("POST", "/memories", body)
        rows = result.get("results", []) if isinstance(result, dict) else result
        return [r["id"] for r in rows if isinstance(r, dict) and r.get("id")]

    def search(self, query: str, metadata_filters: dict | None = None,
               top_k: int = 8, threshold: float | None = None) -> list[dict]:
        filters: dict = {"user_id": self.user_id}
        if metadata_filters:
            filters.update(metadata_filters)
        body: dict = {"query": query, "filters": filters, "top_k": top_k}
        if threshold is not None:
            body["threshold"] = threshold
        result = self._request("POST", "/search", body)
        return _normalize(result)

    def query_all(self, metadata_filters: dict | None = None,
                  top_k: int = 1000) -> list[dict]:
        """按 metadata 条件服务端过滤查询（threshold=0，不做语义筛除）。"""
        return self.search(".", metadata_filters, top_k=top_k, threshold=0.0)

    def get_one(self, memory_id: str) -> dict | None:
        try:
            result = self._request("GET", f"/memories/{urllib.parse.quote(memory_id)}")
        except Mem0Error:
            return None
        row = None
        if isinstance(result, dict):
            if "id" in result:
                row = result
            else:
                rows = result.get("results") or []
                row = rows[0] if rows else None
        if not isinstance(row, dict) or not row.get("id"):
            return None
        return {"id": row["id"],
                "text": row.get("memory") or row.get("data") or "",
                "metadata": row.get("metadata") or {}}

    def update_metadata(self, memory_id: str, metadata_merge: dict) -> None:
        """合并式更新 metadata（先读全量再整体写回，兼容服务端 replace 语义）。"""
        row = self.get_one(memory_id)
        if row is None:
            raise Mem0Error(f"memory {memory_id} not found for update")
        merged = dict(row["metadata"])
        merged.update({k: v for k, v in metadata_merge.items() if v is not None})
        self._request("PUT", f"/memories/{urllib.parse.quote(memory_id)}",
                      {"metadata": merged})

    def list_all(self) -> list[dict]:
        """拉取当前用户全部记忆（>1000 条时需改分页策略）。"""
        q = urllib.parse.urlencode({"user_id": self.user_id, "top_k": 1000})
        return _normalize(self._request("GET", f"/memories?{q}"))

    def delete(self, memory_id: str) -> None:
        self._request("DELETE", f"/memories/{urllib.parse.quote(memory_id)}")

    def delete_many(self, ids: list[str]) -> int:
        n = 0
        for mid in ids:
            self.delete(mid)
            n += 1
        return n

    def find_near_duplicate(self, content: str, project_id: str | None,
                            threshold: float) -> tuple[dict | None, float]:
        hits = self.search(content, {"project_id": project_id} if project_id else None,
                           top_k=1)
        if hits and (hits[0].get("score") or 0) >= threshold:
            return hits[0], hits[0]["score"]
        return None, 0.0


def _normalize(result: dict | list) -> list[dict]:
    rows = result.get("results") if isinstance(result, dict) else result
    out = []
    for r in rows or []:
        if not isinstance(r, dict) or not r.get("id"):
            continue
        out.append({
            "id": r["id"],
            "text": r.get("memory") or r.get("data") or "",
            "score": r.get("score"),
            "created_at": r.get("created_at") or (r.get("metadata") or {}).get("created_at"),
            "metadata": r.get("metadata") or {},
        })
    return out


# ---------------------------------------------------------------------------
# 文本工具：脱敏 + project_id 归一化
# ---------------------------------------------------------------------------
def redact(text: str) -> str:
    """写入共享记忆前的凭证脱敏（与 pi-memory-mem0 privacy 实践对齐并增强）。"""
    text = re.sub(
        r"-----BEGIN [^-]+ PRIVATE KEY-----[\s\S]*?-----END [^-]+ PRIVATE KEY-----",
        "[REDACTED PRIVATE KEY]", text, flags=re.I)
    text = re.sub(r"\b(Bearer)\s+[A-Za-z0-9._~+/=-]{12,}", r"\1 [REDACTED]",
                  text, flags=re.I)
    text = re.sub(
        r"\b(api[_-]?key|token|password|secret|passwd|credential)\s*[:=]\s*[\"']?[^\s\"',;]{6,}",
        r"\1=[REDACTED]", text, flags=re.I)
    text = re.sub(r"\b(?:sk|gh[opusr]|github_pat|xox[baprs])-[-A-Za-z0-9_]{12,}\b",
                  "[REDACTED TOKEN]", text)
    text = re.sub(r"\bAKIA[0-9A-Z]{16}\b", "[REDACTED AWS KEY]", text)
    text = re.sub(r"\beyJ[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}\b",
                  "[REDACTED JWT]", text)
    text = re.sub(r"([a-z][a-z0-9+.-]*://)[^\s/@:]+:[^\s/@]+@", r"\1[REDACTED]@",
                  text, flags=re.I)
    return text


def normalize_remote(url: str) -> str:
    """git remote URL -> 标准化 repo id，如 github.com/yves/my-project。"""
    url = url.strip()
    if url.startswith("git@"):
        url = url.split("git@", 1)[1]
    url = re.sub(r"^(ssh|https?|git)://", "", url)
    if "://" not in url and ":" in url.split("/", 1)[0]:
        url = url.replace(":", "/", 1)  # scp 风格 host:path
    url = url.split("#", 1)[0]
    if url.endswith(".git"):
        url = url[:-4]
    return url.strip("/")


def detect_project_id(repo_path: str | None) -> tuple[str, str]:
    """返回 (project_id, origin)。无 remote 时回退 local-<pathhash>。"""
    cwd = repo_path or "."
    origin = ""
    try:
        r = subprocess.run(["git", "-C", cwd, "remote", "get-url", "origin"],
                           capture_output=True, text=True, timeout=10, check=True)
        origin = r.stdout.strip()
    except (subprocess.SubprocessError, OSError):
        origin = ""  # 无 remote 或非 git 目录，走本地路径哈希回退
    if origin:
        return normalize_remote(origin), origin
    root = subprocess.run(["git", "-C", cwd, "rev-parse", "--show-toplevel"],
                          capture_output=True, text=True, timeout=10)
    base = root.stdout.strip() if root.returncode == 0 else cwd
    return "local-" + hashlib.sha256(base.encode()).hexdigest()[:12], ""


# ---------------------------------------------------------------------------
# FastAPI 服务
# ---------------------------------------------------------------------------
client = Mem0Client(CONFIG["mem0_base_url"], CONFIG["mem0_api_key"], CONFIG["user_id"])

app = FastAPI(title="agent-memory bridge", docs_url=None, redoc_url=None)


def _auth(authorization: str | None) -> None:
    if not authorization or not authorization.startswith("Bearer "):
        raise HTTPException(401, "missing bearer token")
    if not hmac.compare_digest(authorization[7:], TOKEN):
        raise HTTPException(401, "invalid token")


class ContextReq(BaseModel):
    project_id: str | None = None


class SearchReq(BaseModel):
    query: str
    project_id: str | None = None
    type: str | None = None
    status: str = Field(default="active",
                        pattern="^(active|superseded|deprecated|all)$")
    top_k: int = Field(default=8, ge=1, le=50)


class RememberReq(BaseModel):
    content: str = Field(min_length=1, max_length=20000)
    type: str = Field(default="decision", pattern="^(profile|project|decision|task|incident)$")
    scope: str = Field(default="project", pattern="^(global|project)$")
    project_id: str | None = None
    source_agent: str = "cli"
    repo: str | None = None
    branch: str | None = None
    task_id: str | None = None
    tags: list[str] | None = None
    parallel_with: list[str] | None = None


class CheckpointReq(BaseModel):
    repo_path: str
    output_text: str = ""
    source_agent: str = "cli"
    task_id: str | None = None
    started_at: str | None = None
    parallel_agents: list[str] | None = None


class ForgetReq(BaseModel):
    project_id: str | None = None
    type: str | None = None
    source_agent: str | None = None
    dry_run: bool = True


@app.get("/health")
def health():
    return {"ok": True, "user_id": CONFIG["user_id"]}


def _meta(base: dict) -> dict:
    m: dict = {"schema_version": 1, "source_system": "bridge", "created_by": "memory-bridge"}
    m.update({k: v for k, v in base.items() if v not in (None, "", [])})
    return m


def _remember(req: RememberReq) -> dict:
    content = redact(req.content.strip())
    if not content:
        raise HTTPException(400, "empty content after redaction")
    project_id = req.project_id
    if req.scope == "project" and not project_id and req.repo:
        project_id, _ = detect_project_id(req.repo)
    # 确定性去重：mem0 存储 embedding 含 metadata，同文本向量查询仅 ~0.5 分，
    # 向量近重不可靠；改用服务端 content_hash 精确过滤（仅对 active 记忆判重）。
    chash = hashlib.sha256(content.encode()).hexdigest()
    dedup_filters = {"content_hash": chash, "status": "active"}
    if req.scope == "project" and project_id:
        dedup_filters["project_id"] = project_id
    if client.search(".", dedup_filters, top_k=1, threshold=0.0):
        return {"status": "skipped",
                "id": client.search(".", dedup_filters, top_k=1, threshold=0.0)[0]["id"],
                "score": 1.0}
    ids = client.add(content, _meta({
        "content_hash": chash, "status": "active",
        "type": req.type, "scope": req.scope, "project_id": project_id,
        "source_agent": req.source_agent, "repo": req.repo,
        "branch": req.branch, "task_id": req.task_id, "tags": req.tags,
        "parallel_with": req.parallel_with}))
    if not ids:
        raise HTTPException(502, "mem0 add returned no id")
    return {"status": "created", "id": ids[0]}


@app.post("/remember")
def remember(req: RememberReq, authorization: str | None = Header(None)):
    _auth(authorization)
    try:
        return _remember(req)
    except Mem0Error as e:
        raise HTTPException(502, str(e)) from e


@app.post("/search")
def search(req: SearchReq, authorization: str | None = Header(None)):
    _auth(authorization)
    meta: dict = {}
    if req.project_id:
        meta["project_id"] = req.project_id
    if req.type:
        meta["type"] = req.type
    if req.status != "all":
        meta["status"] = req.status
    try:
        hits = client.search(redact(req.query), meta or None, req.top_k)
    except Mem0Error as e:
        raise HTTPException(502, str(e)) from e
    return {"results": [{
        "id": h["id"], "score": h["score"], "type": h["metadata"].get("type"),
        "project_id": h["metadata"].get("project_id"),
        "status": h["metadata"].get("status", "active"),
        "created_at": h["created_at"], "text": h["text"],
    } for h in hits]}


@app.delete("/memory/{memory_id}")
def forget_one(memory_id: str, authorization: str | None = Header(None)):
    _auth(authorization)
    try:
        client.delete(memory_id)
    except Mem0Error as e:
        raise HTTPException(502, str(e)) from e
    return {"deleted": memory_id}


@app.post("/forget")
def forget_many(req: ForgetReq, authorization: str | None = Header(None)):
    """批量删除（污染应急处置）。默认 dry_run，只返回将删除的 id。"""
    _auth(authorization)
    if not (req.project_id or req.type or req.source_agent):
        raise HTTPException(400, "refusing unscoped batch forget")
    rows = client.list_all()
    victims = [r for r in rows if (
        (not req.project_id or r["metadata"].get("project_id") == req.project_id)
        and (not req.type or r["metadata"].get("type") == req.type)
        and (not req.source_agent or r["metadata"].get("source_agent") == req.source_agent))]
    out = {"matched": len(victims), "ids": [v["id"] for v in victims]}
    if req.dry_run:
        out["deleted"] = 0
        return out
    out["deleted"] = client.delete_many(out["ids"])
    return out


@app.post("/context")
def context(req: ContextReq, authorization: str | None = Header(None)):
    _auth(authorization)
    pid = req.project_id

    def fetch(mtype: str, limit: int, project_scoped: bool) -> list[dict]:
        """按条件服务端过滤（type/status/project），阈值 0 不做语义筛除。"""
        base = {"type": mtype, "status": "active"}
        if project_scoped and pid:
            rows = client.query_all({**base, "project_id": pid})
            globals_rows = client.query_all({**base, "scope": "global"})
            rows += [r for r in globals_rows if not r["metadata"].get("project_id")]
        elif project_scoped:
            rows = client.query_all({**base, "scope": "global"})
        else:
            rows = client.query_all(base)
        rows.sort(key=lambda r: r.get("created_at") or "", reverse=True)
        return rows[:limit]

    sections = []
    if pid:
        sections.append(("# Project memory", fetch("project", LIMITS["project"], True)))
        sections.append(("# Recent decisions", fetch("decision", LIMITS["decision"], True)))
        sections.append(("# Current tasks", fetch("task", LIMITS["task"], True)))
        sections.append(("# Known issues", fetch("incident", LIMITS["incident"], True)))
    sections.append(("# User preferences", fetch("profile", LIMITS["profile"], False)))

    lines = ["# Shared agent memory (auto-generated)"]
    empty = True
    for title, items in sections:
        if not items:
            continue
        empty = False
        lines.extend(["", title])
        for r in items:
            src = r["metadata"].get("source_agent", "?")
            lines.append(f"- [{src}] {r['text']}")
    if empty:
        lines.extend(["", "(no memories yet)"])
    total = sum(len(items) for _, items in sections)
    return {"markdown": "\n".join(lines), "project_id": pid, "total": total}


class Mem0LLMError(Exception):
    pass


def _git(repo: str, *args: str, cap: int = 12000) -> str:
    r = subprocess.run(["git", "-C", repo, *args],
                       capture_output=True, text=True, timeout=15)
    return r.stdout.strip()[:cap] if r.returncode == 0 else ""


def _changed_files(repo: str, since: float | None) -> list[str]:
    """diff+untracked 文件列表；给了 since 则按 mtime 过滤出本会话改动的文件。"""
    names = set(_git(repo, "diff", "HEAD", "--name-only", cap=40000).splitlines())
    for line in _git(repo, "status", "--porcelain", cap=20000).splitlines():
        if line.startswith("??") and len(line) > 3:
            names.add(line[3:].strip().strip('"'))
    names.discard("")
    ordered = sorted(names)
    if since is None:
        return ordered[:200]
    keep: list[str] = []
    for n in ordered:
        fp = os.path.join(repo, n)
        try:
            if os.path.getmtime(fp) >= since:
                keep.append(n)
        except OSError:
            keep.append(n)  # 已删除/无法 stat：保留，宁多勿漏
    return keep[:200]


def _filtered_status(repo: str, changed: list[str]) -> str:
    if not changed:
        return ""
    cset = set(changed)
    full = _git(repo, "status", "--porcelain", cap=20000)
    keep = [ln for ln in full.splitlines()
            if len(ln) > 3 and ln[3:].strip().strip('"') in cset]
    return "\n".join(keep)[:2000]


def _parse_since(started_at: str | None) -> float | None:
    if not started_at:
        return None
    try:
        return datetime.fromisoformat(started_at.replace("Z", "+00:00")).timestamp()
    except ValueError:
        return None


@app.post("/checkpoint")
def checkpoint(req: CheckpointReq, authorization: str | None = Header(None)):
    _auth(authorization)
    repo = os.path.abspath(os.path.expanduser(req.repo_path))
    if not os.path.isdir(repo):
        raise HTTPException(400, f"repo not found: {repo}")
    project_id, _origin = detect_project_id(repo)
    since = _parse_since(req.started_at)
    changed = _changed_files(repo, since)
    branch = _git(repo, "rev-parse", "--abbrev-ref", "HEAD", cap=200)
    recent = _git(repo, "log", "--oneline", "-5", cap=1000)
    status = _filtered_status(repo, changed)
    diff_stat = _git(repo, "diff", "HEAD", "--stat", "--", *changed, cap=1500) if changed else ""
    diff = _git(repo, "diff", "HEAD", "--", *changed, cap=12000) if changed else ""
    output = req.output_text[:16000]
    if not output.strip() and not diff and not status:
        return {"project_id": project_id, "branch": branch, "created": [], "skipped": 0,
                "note": "no diff and no output_text; skipped LLM extraction"}
    parallel_note = (
        "检测到同时段并行会话：" + ", ".join(req.parallel_agents) +
        "。diff 可能混入其它会话的改动，只提取可明确归属的信息，不要猜测归属。"
    ) if req.parallel_agents else "无"

    prompt = PROMPT_PATH.read_text().format(
        project_id=project_id, branch=branch or "?", recent=recent or "(none)",
        status=status or "(clean)", diff_stat=diff_stat or "(none)",
        diff=diff or "(no unstaged diff)", output=output or "(none)",
        parallel_note=parallel_note)

    try:
        raw = _llm(prompt)
        extracted = json.loads(_json_block(raw))
    except (Mem0LLMError, ValueError) as e:
        raise HTTPException(502, f"LLM extraction failed: {e}") from e

    written, skipped = [], 0
    for key, mtype in (("decisions", "decision"), ("incidents", "incident"),
                       ("completed", "task"), ("next_steps", "task"), ("profile", "profile")):
        for text in extracted.get(key, []) or []:
            if not isinstance(text, str) or len(text.strip()) < 4:
                continue
            r = _remember(RememberReq(
                content=text, type=mtype, scope="project", project_id=project_id,
                source_agent=req.source_agent, repo=repo, branch=branch or None,
                task_id=req.task_id, parallel_with=req.parallel_agents))
            if r["status"] == "created":
                written.append({"id": r["id"], "type": mtype, "text": text})
            else:
                skipped += 1
    return {"project_id": project_id, "branch": branch, "created": written, "skipped": skipped}


def _llm(prompt: str) -> str:
    body = json.dumps({
        "model": DS["model"], "temperature": 0, "max_tokens": 2000,
        "messages": [{"role": "user", "content": prompt}],
    }).encode()
    req = urllib.request.Request(
        DS["base_url"].rstrip("/") + "/chat/completions", data=body, method="POST",
        headers={"Content-Type": "application/json",
                 "Authorization": "Bearer " + DS["api_key"]})
    try:
        with NO_PROXY_OPENER.open(req, timeout=90) as resp:
            data = json.load(resp)
        return data["choices"][0]["message"]["content"]
    except (urllib.error.HTTPError, urllib.error.URLError, TimeoutError,
            KeyError, json.JSONDecodeError) as e:
        raise Mem0LLMError(str(e)) from e


def _json_block(text: str) -> str:
    text = re.sub(r"```(?:json)?", "", text)
    start, end = text.find("{"), text.rfind("}")
    if start < 0 or end <= start:
        raise ValueError("no JSON object in LLM output")
    return text[start:end + 1]


# ---------------------------------------------------------------------------
# 管理 API：agent 接入/移除、模型配置、记忆浏览
# ---------------------------------------------------------------------------
import shutil  # noqa: E402  (管理功能使用，置于分区头部便于整体移除)

HOOK_DIR = Path(__file__).resolve().parent.parent / "hooks"
ADMIN_HTML = Path(__file__).resolve().parent / "admin.html"
PRESET_PATHS = {
    "codex": Path.home() / ".codex" / "hooks.json",
    "kimi": Path.home() / ".kimi-code" / "config.toml",
    "claude": Path.home() / ".claude" / "settings.json",
}
WIRE_EVENTS = (("SessionStart", "session_context_hook", 15),
               ("SessionEnd", "session_end_hook", 10))


def _hook_cmd(script: str, agent: str) -> str:
    return f"python3 '{HOOK_DIR / script}.py' {agent}"


def _wire_marker(agent: str) -> str:
    return f"_hook.py' {agent}"


def _backup(path: Path) -> None:
    if path.exists():
        bak = path.with_name(path.name + ".bak-agent-memory")
        if not bak.exists():
            shutil.copy2(path, bak)


def _wired(hook_type: str, agent: str) -> bool:
    if hook_type not in PRESET_PATHS:
        return False
    p = PRESET_PATHS[hook_type]
    if not p.exists():
        return False
    return _wire_marker(agent) in p.read_text(errors="replace")


def _json_hooks_load(path: Path) -> dict:
    if path.exists():
        try:
            return json.loads(path.read_text())
        except (OSError, json.JSONDecodeError) as e:
            # 文件损坏时大声失败：绝不能用空配置覆盖用户已有的 hooks（如 herdr 的）
            raise Mem0Error(f"cannot parse {path}: {e}") from e
    return {"hooks": {}}


def _wire_json_style(path: Path, agent: str) -> None:
    data = _json_hooks_load(path)
    hooks = data.setdefault("hooks", {})
    for event, script, timeout in WIRE_EVENTS:
        arr = hooks.setdefault(event, [])
        cmd = _hook_cmd(script, agent)
        if any(cmd in m.get("hooks", [{}])[0].get("command", "") for m in arr):
            continue
        arr.append({"hooks": [{"type": "command", "command": cmd, "timeout": timeout}]})
    _backup(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n")


def _unwire_json_style(path: Path, agent: str) -> None:
    if not path.exists():
        return
    data = _json_hooks_load(path)
    hooks = data.get("hooks", {})
    for event in list(hooks):
        kept = [m for m in hooks[event]
                if not any(_wire_marker(agent) in h.get("command", "")
                           for h in m.get("hooks", []))]
        if kept:
            hooks[event] = kept
        else:
            del hooks[event]
    _backup(path)
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n")


def _wire_kimi(agent: str) -> None:
    p = PRESET_PATHS["kimi"]
    p.parent.mkdir(parents=True, exist_ok=True)
    text = p.read_text() if p.exists() else ""
    if _wire_marker(agent) in text:
        return
    _backup(p)
    blocks = ""
    for event, script, timeout in WIRE_EVENTS:
        blocks += (f'\n[[hooks]]\nevent = "{event}"\n'
                   f'command = "{_hook_cmd(script, agent)}"\n'
                   f"timeout = {timeout}\n")
    p.write_text(text.rstrip() + "\n" + blocks)


def _unwire_kimi(agent: str) -> None:
    p = PRESET_PATHS["kimi"]
    if not p.exists():
        return
    lines = p.read_text().splitlines(keepends=True)
    out: list[str] = []
    i, n = 0, len(lines)
    while i < n:
        if lines[i].strip() == "[[hooks]]":
            j = i + 1
            block = [lines[i]]
            while j < n and not lines[j].lstrip().startswith("["):
                block.append(lines[j])
                j += 1
            if _wire_marker(agent) in "".join(block):
                i = j  # 丢弃本 block
                continue
            out.extend(block)
            i = j
        else:
            out.append(lines[i])
            i += 1
    _backup(p)
    p.write_text("".join(out))


def _wire(hook_type: str, agent: str) -> None:
    if hook_type == "codex":
        _wire_json_style(PRESET_PATHS["codex"], agent)
    elif hook_type == "claude":
        _wire_json_style(PRESET_PATHS["claude"], agent)
    elif hook_type == "kimi":
        _wire_kimi(agent)


def _unwire(hook_type: str, agent: str) -> None:
    if hook_type == "codex":
        _unwire_json_style(PRESET_PATHS["codex"], agent)
    elif hook_type == "claude":
        _unwire_json_style(PRESET_PATHS["claude"], agent)
    elif hook_type == "kimi":
        _unwire_kimi(agent)


def _apply_runtime(cfg: dict) -> None:
    global CONFIG, LIMITS, DEDUP_THRESHOLD, DS, client
    CONFIG = cfg
    LIMITS = cfg.get("context_limits", LIMITS)
    try:
        DEDUP_THRESHOLD = float(cfg.get("dedup_threshold") or 0.92)
    except (TypeError, ValueError) as e:
        raise RuntimeError("dedup_threshold must be a number") from e
    DS = cfg.get("llm") or cfg.get("deepseek", DS)
    client = Mem0Client(cfg["mem0_base_url"], cfg["mem0_api_key"], cfg["user_id"])


def _mask(key: str) -> str:
    key = key or ""
    return ("••••" + key[-4:]) if len(key) >= 8 else "••••"


def _current_llm() -> dict:
    return CONFIG.get("llm") or CONFIG.get("deepseek", {})


@app.get("/", include_in_schema=False)
def admin_page():
    return FileResponse(ADMIN_HTML)


class AgentCreateReq(BaseModel):
    name: str = Field(pattern="^[a-z0-9_-]{1,32}$")
    hook_type: str = Field(default="none", pattern="^(codex|kimi|claude|none)$")


@app.get("/agents")
def list_agents(authorization: str | None = Header(None)):
    _auth(authorization)
    out = []
    for name, a in CONFIG.get("agents", {}).items():
        ht = a.get("hook_type", "none")
        wired = _wired(ht, name) if ht in PRESET_PATHS else None
        out.append({"name": name, "enabled": a.get("enabled", True),
                    "hook_type": ht, "wired": wired})
    return {"agents": out}


@app.post("/agents")
def add_agent(req: AgentCreateReq, authorization: str | None = Header(None)):
    _auth(authorization)
    agents = CONFIG.setdefault("agents", {})
    if req.name in agents:
        raise HTTPException(409, "agent already exists")
    agents[req.name] = {"enabled": True, "hook_type": req.hook_type}
    _save_config(CONFIG)
    return {"ok": True, "name": req.name}


@app.delete("/agents/{name}")
def remove_agent(name: str, authorization: str | None = Header(None)):
    _auth(authorization)
    agents = CONFIG.get("agents", {})
    if name not in agents:
        raise HTTPException(404, "agent not found")
    a = agents[name]
    if a.get("hook_type") in PRESET_PATHS and _wired(a["hook_type"], name):
        _unwire(a["hook_type"], name)
    del agents[name]
    _save_config(CONFIG)
    return {"ok": True, "removed": name}


@app.post("/agents/{name}/wire")
def wire_agent(name: str, authorization: str | None = Header(None)):
    _auth(authorization)
    a = CONFIG.get("agents", {}).get(name)
    if not a:
        raise HTTPException(404, "agent not found")
    if a.get("hook_type") not in PRESET_PATHS:
        raise HTTPException(400, "hook_type does not support wiring")
    try:
        _wire(a["hook_type"], name)
    except Mem0Error as e:
        raise HTTPException(502, str(e)) from e
    return {"ok": True, "name": name, "wired": True}


@app.post("/agents/{name}/unwire")
def unwire_agent(name: str, authorization: str | None = Header(None)):
    _auth(authorization)
    a = CONFIG.get("agents", {}).get(name)
    if not a:
        raise HTTPException(404, "agent not found")
    if a.get("hook_type") in PRESET_PATHS:
        try:
            _unwire(a["hook_type"], name)
        except Mem0Error as e:
            raise HTTPException(502, str(e)) from e
    return {"ok": True, "name": name, "wired": False}


class LlmReq(BaseModel):
    base_url: str = ""
    model: str = ""
    api_key: str | None = None


@app.get("/config")
def get_config(authorization: str | None = Header(None)):
    _auth(authorization)
    llm = _current_llm()
    return {
        "user_id": CONFIG["user_id"],
        "mem0_base_url": CONFIG["mem0_base_url"],
        "llm": {"base_url": llm.get("base_url"), "model": llm.get("model"),
                "api_key_masked": _mask(llm.get("api_key", ""))},
        "limits": {"dedup_threshold": DEDUP_THRESHOLD, "context_limits": LIMITS},
        "agents": CONFIG.get("agents", {}),
    }


@app.post("/config/llm")
def set_llm(req: LlmReq, authorization: str | None = Header(None)):
    _auth(authorization)
    if not req.base_url.strip() or not req.model.strip():
        raise HTTPException(400, "base_url and model required")
    key = (req.api_key or "").strip()
    llm = {"base_url": req.base_url.strip().rstrip("/"),
           "model": req.model.strip(),
           "api_key": key or _current_llm().get("api_key", "")}
    if not llm["api_key"]:
        raise HTTPException(400, "api_key required (none stored yet)")
    CONFIG["llm"] = llm
    _save_config(CONFIG)
    _apply_runtime(CONFIG)
    return {"ok": True, "model": llm["model"], "api_key_masked": _mask(llm["api_key"])}


@app.post("/config/llm/test")
def test_llm(req: LlmReq | None = None, authorization: str | None = Header(None)):
    _auth(authorization)
    src = req if (req and (req.base_url.strip() or req.model.strip())) else None
    cur = _current_llm()
    base = ((src.base_url if src else "") or cur.get("base_url", "")).rstrip("/")
    model = ((src.model if src else "") or cur.get("model", ""))
    key = ((src.api_key or "").strip() if src else "") or cur.get("api_key", "")
    if not (base and model and key):
        raise HTTPException(400, "base_url/model/api_key needed")
    body = json.dumps({"model": model, "max_tokens": 8,
                       "messages": [{"role": "user", "content": "ping"}]}).encode()
    r = urllib.request.Request(
        base + "/chat/completions", data=body, method="POST",
        headers={"Content-Type": "application/json", "Authorization": "Bearer " + key})
    t0 = time.time()
    try:
        with NO_PROXY_OPENER.open(r, timeout=25) as resp:
            data = json.load(resp)
        reply = data["choices"][0]["message"]["content"][:40]
        return {"ok": True, "latency_ms": int((time.time() - t0) * 1000), "reply": reply}
    except urllib.error.HTTPError as e:
        return {"ok": False, "status": e.code,
                "error": e.read().decode(errors="replace")[:200]}
    except Exception as e:
        return {"ok": False, "error": str(e)[:200]}


class LimitsReq(BaseModel):
    dedup_threshold: float | None = Field(default=None, ge=0.5, le=1.0)
    context_limits: dict | None = None


@app.post("/config/limits")
def set_limits(req: LimitsReq, authorization: str | None = Header(None)):
    _auth(authorization)
    if req.dedup_threshold is not None:
        CONFIG["dedup_threshold"] = req.dedup_threshold
    if req.context_limits:
        CONFIG["context_limits"] = req.context_limits
    _save_config(CONFIG)
    _apply_runtime(CONFIG)
    return {"ok": True, "limits": {"dedup_threshold": DEDUP_THRESHOLD,
                                   "context_limits": LIMITS}}


@app.get("/memories")
def browse_memories(authorization: str | None = Header(None),
                    project_id: str | None = None,
                    type: str | None = None,
                    status: str = "active",
                    q: str | None = None,
                    limit: int = 50):
    _auth(authorization)
    rows = client.list_all()
    out = []
    for r in rows:
        m = r["metadata"]
        if status != "all" and m.get("status", "active") != status:
            continue
        if project_id and m.get("project_id") != project_id:
            continue
        if type and m.get("type") != type:
            continue
        if q and q.lower() not in r["text"].lower():
            continue
        out.append({"id": r["id"], "text": r["text"], "metadata": m,
                    "status": m.get("status", "active"),
                    "created_at": r.get("created_at")})
        if len(out) >= limit:
            break
    return {"memories": out, "total_scanned": len(rows)}


# ---------------------------------------------------------------------------
# 生命周期（active/superseded/deprecated）+ 语义整理
# ---------------------------------------------------------------------------
class StatusReq(BaseModel):
    status: str = Field(pattern="^(active|superseded|deprecated)$")
    superseded_by: str | None = None


@app.post("/memory/{memory_id}/status")
def set_memory_status(memory_id: str, req: StatusReq,
                      authorization: str | None = Header(None)):
    """切换记忆生命周期状态；superseded 需提供 superseded_by 指向新记忆。"""
    _auth(authorization)
    if req.status == "superseded" and not req.superseded_by:
        raise HTTPException(400, "superseded requires superseded_by")
    try:
        client.update_metadata(memory_id, {"status": req.status,
                                           "superseded_by": req.superseded_by})
    except Mem0Error as e:
        raise HTTPException(502, str(e)) from e
    return {"ok": True, "id": memory_id, "status": req.status}


@app.post("/admin/backfill-status")
def backfill_status(authorization: str | None = Header(None)):
    """一次性迁移：给缺 status 的历史记忆补 status=active。"""
    _auth(authorization)
    rows = client.list_all()
    n = 0
    for r in rows:
        if not r["metadata"].get("status"):
            try:
                client.update_metadata(r["id"], {"status": "active"})
                n += 1
            except Mem0Error:
                continue
    return {"ok": True, "scanned": len(rows), "backfilled": n}


class ConsolidateReq(BaseModel):
    project_id: str | None = None
    threshold: float = Field(default=0.78, ge=0.5, le=0.99)
    dry_run: bool = True
    max_groups: int = Field(default=10, ge=1, le=50)
    scan: int = Field(default=80, ge=10, le=300)


@app.post("/consolidate")
def consolidate(req: ConsolidateReq, authorization: str | None = Header(None)):
    """语义整理：自检索聚类近似重复组，LLM 判定合并，旧记忆标记 superseded。"""
    _auth(authorization)
    rows = client.list_all()
    act = [r for r in rows
           if r["metadata"].get("status", "active") == "active"
           and (not req.project_id or r["metadata"].get("project_id") == req.project_id)]
    act.sort(key=lambda r: r.get("created_at") or "", reverse=True)
    act = act[:req.scan]

    parent = {r["id"]: r["id"] for r in act}
    by_id = {r["id"]: r for r in act}

    def find(x: str) -> str:
        while parent[x] != x:
            parent[x] = parent[parent[x]]
            x = parent[x]
        return x

    # 词法聚类：字符二元组 containment 找候选近重复对（不用向量检索——
    # mem0 存储 embedding 含 metadata，自检索分数失真），再由 LLM 逐组判定。
    def bigrams(s: str) -> set[str]:
        s = re.sub(r"\s+", "", s)
        return {s[i:i + 2] for i in range(len(s) - 1)} or {s}

    grams = {r["id"]: bigrams(r["text"]) for r in act}
    ids = [r["id"] for r in act]
    for i in range(len(ids)):
        for j in range(i + 1, len(ids)):
            a, b = grams[ids[i]], grams[ids[j]]
            if not a or not b:
                continue
            # containment 对中文改写句稳健：实测近重复对 ≈0.59，不同事实 ≤0.05
            contain = len(a & b) / min(len(a), len(b))
            if contain >= 0.5 and by_id[ids[i]]["metadata"].get("type") == \
                    by_id[ids[j]]["metadata"].get("type"):
                ra, rb = find(ids[i]), find(ids[j])
                if ra != rb:
                    parent[rb] = ra

    groups: dict[str, list[dict]] = {}
    for r in act:
        groups.setdefault(find(r["id"]), []).append(r)
    cands = [g for g in groups.values() if len(g) >= 2][:req.max_groups]

    proposals: list[dict] = []
    applied: list[dict] = []
    for g in cands:
        texts = "\n".join(f"- {x['text']}" for x in g)
        prompt = (
            "以下是同一项目下同类型的几条长期记忆，请判断它们是否在表达同一个事实"
            "（重复或换一种说法）。若是，给出合并后的单句（保留最完整的信息）；"
            "若其实是不同事实，merge 设为 false。只输出 JSON，不要解释：\n"
            '{"merge": true, "merged": "合并后的单句", "reason": "简短理由"}\n\n'
            + texts)
        try:
            verdict = json.loads(_json_block(_llm(prompt)))
        except (Mem0LLMError, ValueError) as e:
            proposals.append({"ids": [x["id"] for x in g], "error": str(e)[:120]})
            continue
        if not verdict.get("merge") or not (verdict.get("merged") or "").strip():
            continue
        prop = {"ids": [x["id"] for x in g], "texts": [x["text"] for x in g],
                "merged": verdict["merged"].strip(), "reason": verdict.get("reason", "")}
        proposals.append(prop)
        if not req.dry_run:
            merged_text = redact(prop["merged"])
            m = dict(g[0]["metadata"])
            m["consolidated"] = True
            m["status"] = "active"
            m["content_hash"] = hashlib.sha256(merged_text.encode()).hexdigest()
            new_ids = client.add(merged_text, m)
            if new_ids:
                for x in g:
                    client.update_metadata(x["id"], {"status": "superseded",
                                                     "superseded_by": new_ids[0]})
                applied.append({"new_id": new_ids[0],
                                "superseded": [x["id"] for x in g],
                                "merged": prop["merged"]})
    return {"scanned": len(act), "groups_found": len(cands), "proposals": proposals,
            "applied": applied, "dry_run": req.dry_run}
