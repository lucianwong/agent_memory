# agent-memory

**为多个 AI 编码工具（codex / zcode / kimi / pi / claude…）提供一套共享的长期记忆系统。**
任何一个工具学到的经验、决策、踩坑，其他工具下次开工自动可用。

基于 [mem0](https://github.com/mem0ai/mem0) 自托管服务端，通过一个本地 **Memory Bridge** +
`memory` CLI + **生命周期 hooks 全自动注入/落库**，并附带 **Web 管理台**。

---

## 特性

- 🔌 **多工具共享**：所有记忆存在同一 `user_id` 作用域下，跨工具互通（实测 codex 写入 → pi 可检索）
- ⚡ **全自动 hooks**：SessionStart 自动注入记忆上下文；SessionEnd 异步采集 git 变更 + 会话产出，
  LLM 提取后写回 —— 全程无需人工干预
- 🖥 **Web 管理台**：Agent 接入/移除（自动读写对应工具的 hooks 配置）、提取模型与 API Key 配置（热更新）、
  记忆浏览/删除
- 🔐 **安全**：Bridge 仅绑定 127.0.0.1 + Bearer Token；写入前自动脱敏（私钥/令牌/JWT/URL 凭证）；
  独立 Mem0 API Key 可单独吊销
- 🧹 **确定性去重**：`content_hash` 元数据精确匹配（Mem0 的存储 embedding 含 metadata，
  同文本向量查询仅 ~0.5 分，向量去重不可行 —— 这是实测结论）
- 💾 无状态 Bridge：全部数据在 Mem0，Bridge 可随时重建

## 架构

```text
  codex        zcode       kimi        pi(扩展直连)
    │            │           │              │
    └────────────┴─────┬─────┘              │
                       │ memory CLI          │
                       ▼                     │
               Memory Bridge ◄────────────────┘
          127.0.0.1:8765 (FastAPI + Web 管理台)
        鉴权 · 脱敏 · project_id 归一 · content_hash 去重
                       │ HTTPS + X-API-Key
                       ▼
              自部署 Mem0（Tailscale Serve 等）
              pgvector + embedding + LLM 提取
```

## 快速开始

### 前置条件

- Python 3.10+
- 一个可访问的 Mem0 Self-Hosted 服务端（REST + API Key 鉴权）
- （macOS 可选）launchd 常驻；（Linux 可选）systemd user 单元

### 安装

```bash
git clone https://github.com/lucianwong/agent_memory.git ~/agent_memory
cd ~/agent_memory
./install.sh                 # venv + 依赖 + config.json 初始化 + CLI 软链
./install.sh --with-launchd  # macOS 开机常驻（Linux 用 --with-systemd）
```

编辑 `config.json`（0600）填入你的 Mem0 地址 / API Key / LLM 配置，或启动后打开
`http://127.0.0.1:8765/` 在 Web 管理台填写。

### 接入 Agent

```bash
# 方式一：Web 管理台 → Agent 接入 → 一键接线/移除
open http://127.0.0.1:8765/

# 方式二：CLI 场景下手动接线（codex / kimi / claude 支持自动写入 hooks 配置）
curl -s -X POST -H "Authorization: Bearer $TOKEN" http://127.0.0.1:8765/agents/kimi/wire
```

支持三种接线预设：

| hook_type | 写入位置 | 说明 |
| --- | --- | --- |
| `codex` | `~/.codex/hooks.json` | Claude 风格 hooks（SessionStart/SessionEnd） |
| `kimi` | `~/.kimi-code/config.toml` | `[[hooks]]` 数组 |
| `claude` | `~/.claude/settings.json` | Claude Code hooks |
| `none` | — | 仅注册（如 pi 已有原生扩展、zcode 走 AGENTS.md 指令） |

> codex 首次加载新 hooks 会弹一次 "Hooks need review" 信任门，选 **Trust all and continue**。
> 接线/移除均自动备份原文件（`*.bak-agent-memory`）。

## 使用

```bash
memory context                 # 会话开始：拉取当前项目 + 全局记忆（hooks 已自动做）
memory remember --type decision "决定用 Supabase Storage 存头像"
memory search "认证方案"
memory checkpoint              # 收尾：汇总 git 变更 + 会话产出，LLM 提取写回
memory forget <id>
memory forget-batch --project github.com/yves/myapp          # dry-run
memory forget-batch --project github.com/yves/myapp --execute
```

`project_id` 自动从 `git remote get-url origin` 归一化推导（worktree 共享同一项目记忆），
无 remote 时回退 `local-<路径哈希>`。

## 配置参考（config.json）

| 键 | 说明 |
| --- | --- |
| `bridge_token` | Bridge 与 CLI/管理台的 Bearer Token |
| `mem0_base_url` / `mem0_api_key` | Mem0 服务端地址与专用 Key |
| `user_id` | 记忆作用域（所有工具共用） |
| `llm.base_url` / `llm.model` / `llm.api_key` | checkpoint 提取用的 LLM（OpenAI 兼容） |
| `agents` | Agent 注册表（Web 台管理） |
| `dedup_threshold` / `context_limits` | 检索/去重/上下文配额 |

完整字段见 [`config.example.json`](config.example.json)。

## HTTP API

| 端点 | 说明 |
| --- | --- |
| `GET /health` · `GET /`（管理台） | 健康 / Web UI |
| `GET/POST /agents` · `DELETE /agents/{name}` | Agent 注册表管理 |
| `POST /agents/{name}/wire` · `/unwire` | 接线 / 摘除 hooks |
| `GET/POST /config/llm` · `POST /config/llm/test` | 提取模型配置与连通测试 |
| `GET/POST /config/limits` | 去重与上下文配额 |
| `POST /context` · `POST /search` · `POST /remember` | 记忆核心操作 |
| `POST /checkpoint` · `GET /memories` | 会话落库 · 记忆浏览 |
| `DELETE /memory/{id}` · `POST /forget` | 单条/批量删除（批量默认 dry-run） |

## 安全模型

- Bridge 仅监听 `127.0.0.1`；所有数据端点要求 `Authorization: Bearer <bridge_token>`
- 写入前自动脱敏：私钥块、Bearer token、`api_key=`、`sk-`/`ghp_`/`xox`- 令牌、AKIA、JWT、URL 内嵌凭证
- 记忆文本按不可信数据处理：Web 台渲染走纯 DOM API，无 innerHTML 注入面
- 批量删除强制 dry-run 默认 + 至少一个过滤条件

## 故障排查

| 现象 | 处理 |
| --- | --- |
| Mac 上访问 Tailscale 域名超时 | `curl --noproxy '*'`（Surge 等代理会劫持 `*.ts.net`） |
| Bridge 502 | Mem0 侧故障，检查服务端容器与网络后重试 |
| codex 弹 "Hooks need review" | 一次性信任门，选 Trust all and continue |
| 记忆写错项目 | 检查 `git remote get-url origin`；或显式 `--project` |

## License

[MIT](LICENSE)
