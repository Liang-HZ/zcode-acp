"""zcode-acp — 把 ZCode (app-server 私有协议) 桥接成 ACP (Agent Client Protocol) 服务器。

用途：任何 ACP 客户端都可以把 `zcode-acp` 当作标准 ACP agent 驱动 ZCode；ZCode
继续跑在自己的客户端引擎里、用你自己的渠道配置（~/.zcode/cli/config.json 里配
置的 provider）——本适配器不代理 API、不搬 key。

协议映射（均已实测验证，2026-09-14）：
  ACP initialize                → 透传成功
  ACP session/new {cwd,mcpServers} → session/create {workspace:{workspacePath, workspaceKey},
                                  mcpServers 原样透传(客户端自带 MCP + 用户 MCP)}
                                  + 自动应答 requestRuntimePreferences / requestOfficialMcpAuthHeaders
  ACP session/cancel            → v4/command type=stop（桌面端停止按钮的原生路径，
                                  停整个前台执行；旧版引擎不认时回落 hard_stop 兜底）
  ACP session/prompt            → session/send → session/subscribe 推送流:
                                  model.streaming reasoning_delta → agent_thought_chunk(思考逐 token)
                                  model.streaming text_delta      → agent_message_chunk(正文逐 token)
                                  tool.updated scheduled/started/result → tool_call(_update) 卡片
                                  turn.completed → 轮结束;兜底 session/read(轮基线过滤防历史重放)
  ACP _session/steering         → v4/command {type:"sendText",
                                  payload:{requestedDelivery:"guide"}}（把补充消息
                                  注入**正在跑的那一轮**；回执按引擎的裁决如实分档：
                                  injected / queued(+reasonCode) / promptRequired /
                                  startedNewTurn / failed。详见 README「steering」）
                                  轮次进行中的并发 session/prompt 走同一条通道。
  interaction/requestPermission → ACP session/request_permission（kind 映射），
                                  由客户端决定后回 {decision: allow|deny}；
                                  ZCODE_ACP_APPROVAL_BRIDGE=1 时改走文件式外部审批桥
  环境变量：ZCODE_CJS（默认 /Applications/ZCode.app/.../zcode.cjs）
            ZCODE_ACP_MODEL（如 ark/glm-5.3-flash，session/new 后应用）
            ZCODE_ACP_APPROVAL_BRIDGE=1 + ZCODE_ACP_APPROVAL_DIR
            （可选：文件式外部审批桥，headless / 自研客户端用；见 README）
用法：zcode-acp  （stdio 上的 ACP 服务器）
"""
import json
import os
import re
import selectors
import subprocess
import sys
import tempfile
import threading
import time
import uuid

ZC_CJS = os.environ.get(
    "ZCODE_CJS",
    "/Applications/ZCode.app/Contents/Resources/glm/zcode.cjs")
MODEL = os.environ.get("ZCODE_ACP_MODEL", "")

# 可选的文件式外部审批桥：ZCODE_ACP_APPROVAL_BRIDGE=1 时，权限请求写成 JSON
# 文件落在该目录，外部进程写回裁决（协议见 README「文件式审批桥」）。
# 每次请求时读环境变量，运行时设置也能生效。
def approval_bridge_enabled():
    return os.environ.get("ZCODE_ACP_APPROVAL_BRIDGE") == "1"


def approval_dir():
    return os.path.expanduser(
        os.environ.get("ZCODE_ACP_APPROVAL_DIR") or "~/.zcode-acp/pending")

# ---------------------------------------------------------------- 会话安全模式
#
# 客户端的模式选择器（变更前确认/自动编辑/计划模式/完全访问）来自 ACP 的
# `modes` 声明。之前 shim 没声明 → 选择器根本不出现。引擎原生支持
# plan/build/edit/yolo/auto 五种模式（session/setMode），一一映射过去，
# 让引擎侧权限策略同步生效：
ACP_MODES = [
    {"id": "default", "name": "变更前确认", "description": "改文件前先问我。"},
    {"id": "acceptEdits", "name": "自动编辑", "description": "自动编辑文件。"},
    {"id": "plan", "name": "计划模式", "description": "编辑前先出计划。"},
    {"id": "bypassPermissions", "name": "完全访问", "description": "减少确认次数。"},
]
MODE_TO_ENGINE = {"default": "build", "acceptEdits": "edit",
                  "plan": "plan", "bypassPermissions": "yolo"}
EDIT_TOOLS = {"edit", "write", "multiedit", "notebookedit", "writestdin"}
# `_session/steering` 回执枚举 —— initialize 的 `_meta.steering.outcomes` 广告
# 的就是这一份，和 `_handle_steering` / `ZCode.steer()` 实际可能返回的 outcome
# 一一对应（`failed` 是最终态：引擎拒绝/超时，reason/detail 原样透传）。
# 用例 test_steering_outcomes_enum_matches_implementation 守住两边一致。
STEERING_OUTCOMES = ["injected", "queued", "promptRequired",
                     "startedNewTurn", "failed"]
READ_TOOLS = {"read", "grep", "glob", "ls", "webfetch", "websearch", "todowrite",
              "todoread", "notebookread", "task", "agent", "subagent"}

def engine_mcp_servers(servers):
    """ACP mcpServers → 引擎 app-server 期望的形状（2026-09-16 引擎实测）。

      stdio:    {name, command, args, env:[{name,value}…]}    env 必填（可空数组）
      http/sse: {name, type, url, headers:[{name,value}…]}    headers 必填

    ACP 侧 env/headers 是 map，直接透传会被引擎 ZodError 拒掉**整个**
    session/create（实测 mcpServers.0 invalid_union: expected array）。
    未知字段一律剥掉——引擎 schema 对多出来的键同样报 unrecognized_keys。"""
    def pairs(v):
        if isinstance(v, dict):
            return [{"name": str(k), "value": str(val)} for k, val in v.items()]
        if isinstance(v, list):
            return [{"name": str(x["name"]), "value": str(x.get("value", ""))}
                    for x in v if isinstance(x, dict) and "name" in x]
        return []

    out = []
    for s in servers:
        if not isinstance(s, dict):
            continue
        if s.get("type") in ("http", "sse"):
            out.append({"name": str(s.get("name") or ""), "type": s["type"],
                        "url": str(s.get("url") or ""),
                        "headers": pairs(s.get("headers"))})
        else:
            out.append({"name": str(s.get("name") or ""),
                        "command": str(s.get("command") or ""),
                        "args": [str(a) for a in (s.get("args") or [])],
                        "env": pairs(s.get("env"))})
    return out


# 诊断落盘按进程隔离：同一台机器上常同时跑多个适配器实例（客户端里的会话 +
# 本仓库的测试），共用一个固定文件名时引擎 stderr 是 "w" 打开的，后起的实例
# 会把前一个的日志截断，排障时拿到的是别人的现场。
_LOG_DIR = os.environ.get("ZCODE_ACP_LOG_DIR") or os.path.expanduser("~/.zcode-acp")
try:
    os.makedirs(_LOG_DIR, exist_ok=True)
except Exception:
    _LOG_DIR = tempfile.gettempdir()
TRACE_PATH = os.environ.get("ZCODE_ACP_TRACE_LOG") or \
    os.path.join(_LOG_DIR, f"trace-{os.getpid()}.log")
TRACE_ON = os.environ.get("ZCODE_ACP_TRACE", "1") != "0"
# 引擎 stderr（含 MCP 启动/握手报错）落盘：以前是 DEVNULL，引擎侧出问题时
# shim 只能看到"create 超时"，没有任何线索。
ENGINE_LOG_PATH = os.environ.get("ZCODE_ACP_ENGINE_LOG") or \
    os.path.join(_LOG_DIR, f"engine-{os.getpid()}.log")


def log(msg):
    sys.stderr.write(f"[zcode-acp] {msg}\n")
    sys.stderr.flush()


def trace(msg):
    """诊断日志：客户端与适配器之间的交互不进客户端日志，只能自己落盘留证。"""
    if not TRACE_ON:
        return
    try:
        with open(TRACE_PATH, "a") as f:
            f.write(f"{time.strftime('%H:%M:%S')} {msg}\n")
    except Exception:
        pass


# 引擎 availability 快照（session/create|resume 的 settings.model.available）。
# 模型真值窗口（contextWindow）从这里取，**不要**用 session 投影——实测引擎
# 3.15 的投影 contextWindow 只在会话创建/水合时写一次、之后换模型不更新，
# 会一直停在默认 200000（目录里 glm-5.3-flash=1000000，投影恒为 200000，
# 客户端上下文圆环因此长期按 200K 算）。
MODEL_AVAIL_LIST = []


def capture_availability(result):
    """session/create|resume 的 result → 记录引擎 availability（有序）。"""
    try:
        avail = ((((result or {}).get("settings") or {}).get("model") or {})
                 .get("available") or [])
        entries = [e for e in avail if isinstance(e, dict)]
        if entries:
            MODEL_AVAIL_LIST[:] = entries
    except Exception as e:
        log(f"availability capture failed: {e}")


def catalog_context_window(provider_id, model_id):
    """availability 里某模型的目录 contextWindow；缺失/非法返回 None。"""
    for entry in list(MODEL_AVAIL_LIST):
        ref = (entry.get("ref") or entry) if isinstance(entry, dict) else {}
        if ref.get("providerId") == provider_id and ref.get("modelId") == model_id:
            cw = entry.get("contextWindow")
            if isinstance(cw, (int, float)) and cw > 0:
                return int(cw)
    return None


# ---------------------------------------------------------------- ZCode client

def zcode_model_catalog(current_mode="default"):
    """模型目录：唯一事实源 = 桌面版配置 ~/.zcode/v2/config.json。
    过滤语义与 ZCode 桌面菜单一致：enabled=False、系统禁用（not_entitled/inactive）、
    未填 apiKey 的渠道不显示。用户在 ZCode 客户端的任何增删改/启停，
    下一次 session/new 自动同步——无需在客户端侧做任何事。"""
    if current_mode not in MODE_TO_ENGINE:
        current_mode = "default"
    available, alias = [], {}
    current = None

    def add(pid, pname, mid, label, ctx=None, variants=None):
        value = f"{pname}/{mid}"
        if value in alias:
            return
        alias[value] = (pid, mid, variants)
        available.append({
            "modelId": value,
            "label": label or mid,
            "totalContextTokens": ctx or None,
            **({"supportsReasoningEffort": True} if variants else {}),
        })

    try:
        v2 = json.load(open(os.path.expanduser("~/.zcode/v2/config.json")))
        # 先按桌面菜单顺序：enabled=True 在前，其后 enabled=None（自定义默认可用）
        entries = list((v2.get("provider") or {}).items())
        entries.sort(key=lambda kv: 0 if kv[1].get("enabled") is True else 1)
        for pid, pc in entries:
            if pc.get("enabled") is False:
                continue
            if pc.get("systemDisabledReason"):
                continue
            opts = pc.get("options") or {}
            if opts.get("apiKey") == "" or (pc.get("kind") and "apiKey" not in opts):
                # 未填 key 的渠道不可用（桌面菜单不显示）
                if "apiKey" not in opts:
                    pass
                else:
                    continue
            pname = pc.get("name") or pid
            models = pc.get("models") or {}
            for mid, mc in models.items():
                variants = ((mc.get("reasoning") or {}).get("variants")) or None
                # 与桌面一致：自定义渠道显示模型 ID 原文（name 字段已过期不用）
                add(pid, pname, mid, mid,
                    (mc.get("limit") or {}).get("context"), variants)
            if current is None:
                # 桌面当前选中 = zcode.priority 最高的模型
                by_pri = sorted(models.items(),
                                key=lambda kv: -((kv[1].get("zcode") or {}).get("priority") or 0))
                if by_pri:
                    current = f"{pname}/{by_pri[0][0]}"
    except Exception as e:
        log(f"v2 catalog read failed: {e}")

    env_model = os.environ.get("ZCODE_ACP_MODEL")
    if env_model:
        current = env_model
        if env_model not in alias:
            alias[env_model] = (*env_model.split("/", 1), None)
            available.append({"modelId": env_model, "label": env_model.split("/")[-1]})
    if current is None and available:
        current = available[0]["modelId"]
    if current and current not in alias:
        alias[current] = (*current.split("/", 1), None)
        available.insert(0, {"modelId": current, "label": current.split("/")[-1]})

    catalog = {"availableModels": available, "currentModelId": current, "alias": alias}
    # 模式也进 configOptions（category:"mode"）——生态惯例：claude / codex / cursor /
    # codebuddy / opencode / qoder 等所有提供 configOptions 的 agent 都把模式维度
    # 放进配置项。只发 legacy `modes` 的话，严格按规范实现的客户端会隐藏模式选择器
    # （ACP 规范：
    # "Clients SHOULD use them [configOptions] instead of the modes field"），
    # 用户就再也切不了模式。legacy modes 仍保留（规范要求过渡期双发）。
    cfg_opts = [{
        "id": "mode", "name": "安全模式", "category": "mode", "type": "select",
        "currentValue": current_mode,
        "options": [{"value": m["id"], "name": m["name"],
                     "description": m.get("description")} for m in ACP_MODES],
    }, {
        "id": "model", "name": "Model", "category": "model", "type": "select",
        "currentValue": current,
        "options": [{"value": m["modelId"], "name": m.get("label") or m["modelId"]}
                    for m in available],
    }]
    # 推理档位：所有模型都提供（ZCode 引擎接受任意档位的 setThoughtLevel，实测确认）。
    # ⚠️ availableModels 里每条都要带 supportsReasoningEffort=True —— 之前只在模型
    # 自带 reasoning variants 时才给，客户端据此渲染推理档位按钮 → 换到无 variants
    # 的模型（如 deepseek-flash）按钮就消失了。引擎反正接受任意档位，统一给 True。
    _ORDER = ["low", "medium", "high", "xhigh", "max"]
    name_map = {"low": "低", "medium": "中", "high": "高", "max": "最高", "xhigh": "最高"}
    cur_variants = (alias.get(current) or (None, None, None))[2] or []
    if isinstance(cur_variants, str):
        cur_variants = [cur_variants]
    levels = [v for v in _ORDER if v in cur_variants] or list(_ORDER)
    for v in cur_variants:
        if v not in levels:
            levels.append(v)
    if "medium" not in levels:
        levels.append("medium")
    current_thought = os.environ.get("ZCODE_ACP_THOUGHT", "medium")
    if current_thought not in levels:
        current_thought = levels[0]
    cfg_opts.append({
        "id": "reasoning_effort", "name": "推理强度", "category": "model",
        "type": "select", "currentValue": current_thought,
        "options": [{"value": v, "name": name_map.get(v, v)} for v in levels],
    })
    for m in available:
        m["supportsReasoningEffort"] = True
    catalog["configOptions"] = cfg_opts
    catalog["currentThought"] = current_thought
    return catalog


def sync_channels_to_headless(catalog):
    """把桌面版启用的渠道（v2）同步进 headless 配置（~/.zcode/cli/config.json 的
    provider 树），引擎启动时全部物化 → 客户端里跨渠道切模型原生支持。
    ZCode 客户端里增删改渠道/模型后，下一次 session/new 自动重同步。"""
    cfg_path = os.path.expanduser("~/.zcode/cli/config.json")
    try:
        cfg = json.load(open(cfg_path))
        provs = cfg.get("provider") or {}
        for value, (pid, mid, _v) in catalog.get("alias", {}).items():
            v2 = _v2_provider_def(pid)
            if not v2:
                continue
            pname, kind, base, key, models = v2
            api = "anthropic-messages" if kind == "anthropic" else "openai-chat-completions"
            entry = provs.get(pid) or {}
            entry.update({
                "kind": kind, "api": api, "name": pname,
                "options": {"baseURL": base, "apiKey": key},
                "models": {m: {"name": n} for m, n in models.items()},
            })
            provs[pid] = entry
        cur = catalog.get("currentModelId") or ""
        if cur in catalog.get("alias", {}):
            pid, mid = catalog["alias"][cur][:2]
            cfg["model"] = f"{pid}/{mid}"
        tmp = cfg_path + ".tmp"
        with open(tmp, "w") as f:
            json.dump(cfg, f, ensure_ascii=False, indent=2)
        os.replace(tmp, cfg_path)
    except Exception as e:
        log(f"channel sync failed: {e}")


_V2_CACHE = {}


def _v2_provider_def(pid):
    """读桌面版 v2 里某渠道的定义（缓存）。返回 (name, kind, baseURL, apiKey, models)。"""
    if not _V2_CACHE:
        try:
            v2 = json.load(open(os.path.expanduser("~/.zcode/v2/config.json")))
            for p, pc in (v2.get("provider") or {}).items():
                opts = pc.get("options") or {}
                models = {m: (mc.get("name") or m) for m, mc in (pc.get("models") or {}).items()}
                _V2_CACHE[p] = (pc.get("name") or p, pc.get("kind"),
                                 opts.get("baseURL"), opts.get("apiKey"), models)
        except Exception as e:
            log(f"v2 read failed: {e}")
    return _V2_CACHE.get(pid)


TOOL_KIND = {
    "Read": "read", "Write": "edit", "Edit": "edit", "MultiEdit": "edit",
    "NotebookEdit": "edit", "NotebookRead": "read",
    "Grep": "search", "Glob": "search", "LS": "search", "Search": "search",
    "WebFetch": "fetch", "WebSearch": "fetch", "Fetch": "fetch", "Browser": "fetch",
    "Task": "think", "Agent": "think", "TodoWrite": "think", "Plan": "think",
}


def _tool_kind(name):
    return TOOL_KIND.get(name or "", "execute")


def _tool_title(name, inp):
    """claude-acp 同款标题：Bash=命令原文，文件类=工具+文件名，检索类=pattern。"""
    if not isinstance(inp, dict):
        return (name or "tool").strip() or "tool"
    if name == "Bash" and inp.get("command"):
        return str(inp["command"])[:160]
    for k in ("file_path", "notebook_path", "path"):
        if inp.get(k):
            return f"{name} {os.path.basename(str(inp[k]))}"
    for k in ("pattern", "query", "url", "description", "skill", "command"):
        if inp.get(k):
            return f"{name} {str(inp[k])[:140]}"
    return (name or "tool").strip() or "tool"


def _tool_locations(inp):
    if isinstance(inp, dict):
        for k in ("file_path", "notebook_path", "path"):
            v = inp.get(k)
            if v:
                line = inp.get("offset") if isinstance(inp.get("offset"), int) else 1
                return [{"path": str(v), "line": max(1, line)}]
    return []


def _content_text(text, max_len=1500):
    """ACP 合法的 ToolCallContent：必须包 {type:"content", content:{...}}
    （裸 {type:"text"} 是 ContentBlock，不是 ToolCallContent——严格解析的客户端
    会整条丢弃）。"""
    if not text:
        return []
    return [{"type": "content", "content": {"type": "text", "text": text[:max_len]}}]


def _tool_acp_update(tool_key, info, tool_names):
    """工具轨迹 → ACP update 字典（父/子会话共用；契约=claude-agent-acp）。"""
    tid = f"zc-{tool_key}"
    name = info.get("name") or tool_names.get(str(tool_key), "tool")
    tool_names[str(tool_key)] = name
    phase = info.get("phase")
    inp = info.get("input")
    if phase == "result":
        upd = {"sessionUpdate": "tool_call_update", "toolCallId": tid,
               "status": "completed" if info.get("ok", True) else "failed"}
        content = _content_text(info.get("output") or "")
        if content:
            upd["content"] = content
    elif phase == "refine":
        upd = {"sessionUpdate": "tool_call_update", "toolCallId": tid,
               "title": _tool_title(name, inp), "kind": _tool_kind(name),
               "rawInput": inp if isinstance(inp, dict) else {}}
    elif phase == "started":
        upd = {"sessionUpdate": "tool_call_update", "toolCallId": tid,
               "status": "in_progress", "title": _tool_title(name, inp),
               "kind": _tool_kind(name)}
    else:
        upd = {"sessionUpdate": "tool_call", "toolCallId": tid,
               "title": _tool_title(name, inp), "kind": _tool_kind(name),
               "status": "pending",
               "rawInput": inp if isinstance(inp, dict) else {},
               "content": []}
        locs = _tool_locations(inp)
        if locs:
            upd["locations"] = locs
    return upd


def _is_subagent_tool(info):
    """子代理派发工具（Task/Agent）：主会话不出卡（claude-acp 同款——
    由 subagent_spawned + 子会话流呈现）。"""
    name = (info.get("name") or "").lower()
    return info.get("source") == "subagent" or name in ("task", "agent")


_SUB_TOOLCALL_RE = re.compile(r"^tool_(subagent_[0-9a-fA-F-]{36})_call_")

_META_KEY = {"agentId": "agent_id", "agentType": "agent_type",
             "parentToolCallId": "parent_tool_call_id", "description": "description"}


def _child_sid_from_tool_call(tid):
    """子代理工具 callId 自带子会话身份：tool_<subagent_<uuid>>_call_<hex>
    → 引擎子会话 id = sess_<subagent_<uuid>>。用于第一时间发现子代理
    （早于 session/subagents 轮询）。"""
    if not isinstance(tid, str):
        return None
    m = _SUB_TOOLCALL_RE.match(tid)
    return f"sess_{m.group(1)}" if m else None


def find_node():
    """GUI/受限 PATH 下也能找到 node（nvm/homebrew/系统顺序）。"""
    from shutil import which
    n = which("node")
    if n:
        return n
    import glob
    for pat in ("/Users/*/.nvm/versions/node/*/bin/node",
                "/opt/homebrew/bin/node", "/usr/local/bin/node", "/usr/bin/node"):
        g = sorted(glob.glob(pat), reverse=True)
        if g:
            return g[0]
    return "node"


def engine_env():
    """引擎进程环境（唯一改动点：过长的 TMPDIR 换短目录）。

    引擎启动时会在 TMPDIR 下建 `znr-<uuid>.sock`（桌面 node-repl 中继），
    路径 = len(TMPDIR)+1+45。macOS unix socket 上限 104 字节（含结尾 NUL）
    ——超过时 listen() 直接 EINVAL，引擎立刻崩、会话创建静默卡死。
    某些做沙箱隔离的客户端会给每个 agent 进程指一个很长的私有 TMPDIR
    （实测样本 118 字符）→ 必炸。过长的 TMPDIR 换成 /tmp 下的短目录；
    其余情况原样继承，不打扰。"""
    env = dict(os.environ)
    td = (env.get("TMPDIR") or "/tmp").rstrip("/") or "/"
    if len(td) + 1 + 45 > 103:
        short = "/tmp/zcode-acp-tmp"
        try:
            os.makedirs(short, mode=0o700, exist_ok=True)
            env["TMPDIR"] = short
            log(f"engine TMPDIR 过长（{len(td)}字符）→ 换用 {short}")
        except Exception as e:
            log(f"engine TMPDIR 替换失败（{e}），沿用 {td}")
    return env


def _v2_registry():
    """v2 桌面配置 → 引擎 provider 注册表(resume 前置:恢复渠道可用性)。
    schema 实测:$f/Akn/Nje/mEt——revision 是字符串,generatedAt 是 epoch ms,
    apiKey 是 {source:"inline", value}。"""
    try:
        v2 = json.load(open(os.path.expanduser("~/.zcode/v2/config.json")))
    except Exception as e:
        log(f"v2 registry read failed: {e}")
        return None
    providers = []
    for pid, pc in (v2.get("provider") or {}).items():
        if pc.get("enabled") is False or pc.get("systemDisabledReason"):
            continue
        opts = pc.get("options") or {}
        key = opts.get("apiKey")
        if not key:
            continue
        kind = "anthropic" if pc.get("kind") == "anthropic" else "openai-compatible"
        api = "anthropic-messages" if kind == "anthropic" else "openai-chat-completions"
        models = []
        for mid, mc in (pc.get("models") or {}).items():
            m = {"modelId": mid}
            ctx = ((mc.get("limit") or {}).get("context"))
            out = ((mc.get("limit") or {}).get("output"))
            if ctx:
                m["contextWindow"] = ctx
            if out:
                m["maxOutputTokens"] = out
            models.append(m)
        if not models:
            continue
        providers.append({"providerId": pid, "kind": kind, "apiFormat": api,
                          "label": pc.get("name") or pid, "source": "workspace",
                          "baseURL": opts.get("baseURL"),
                          "apiKey": {"source": "inline", "value": key},
                          "models": models})
    if not providers:
        return None
    return {"revision": f"acp-{int(time.time()*1000)}",
            "generatedAt": int(time.time() * 1000),
            "providers": providers}


class ZCode:
    def __init__(self, cwd):
        self.cwd = cwd
        self._stdin_lock = threading.Lock()   # 多线程共写引擎 stdin，必须串行
        self.proc = subprocess.Popen(
            [find_node(), ZC_CJS, "app-server", "--stdio"], cwd=cwd,
            stdin=subprocess.PIPE, stdout=subprocess.PIPE,
            stderr=open(ENGINE_LOG_PATH, "w"), env=engine_env())
        log(f"zcode engine pid={self.proc.pid} cwd={cwd} stderr→{ENGINE_LOG_PATH}")
        self.log = []
        self.next_id = 1000
        self.session_id = None
        # ACP session/new 透传的客户端 MCP（客户端自带 + 用户配置）。
        # 引擎把它当 per-load runtime 配置、不随会话持久化 → 每次 resume 都要重挂。
        self.mcp_servers = []
        self.workspace = None    # {workspacePath, workspaceKey} — 子会话激活(resume)要用
        # V4 网关(桌面同款实时通道):子会话的 token 级流只能从这里拿。
        # 见 _v4_frame 注释。
        self.v4_conn = None
        self.v4_subs = {}          # topic -> {subscriptionId, logEpoch}
        self.v4_topic_owner = {}   # topic -> 引擎子会话 id
        self.v4_pending = {}       # 请求 id -> topic（用于记录订阅成败）
        self.v4_parent_topic = None
        self.last_v4_frame_ts = 0.0
        self.v4_wc_topic = None    # workspace-config topic(原生斜杠命令列表)
        self.v4_wc_cb = None
        self.slash_commands = []
        self.v4_cb = None          # 当前轮帧回调(读者线程调用)
        self.turn_active = False   # 无活动轮时的权限请求一律直接拒绝（防终止后仍弹框）
        # steering 台账：commandId -> 引擎对这条 guide 投递的裁决过程。
        # 两条来源同时喂它（都来自读者线程）：
        #   · 旧通道 session/event：turn.steerQueued（带 delivery/intent）、turn.steerDrained
        #   · V4 父会话帧 state.updated → patch.queue.items[]（唯一能看到 fellBack 的通道，
        #     因为 TurnSteerDeliveryChanged 在 C6i 映射里落到 default → "session.updated"，
        #     旧通道看不出是降级）
        self._steer = {}
        self._steer_lock = threading.Lock()
        # 会话落盘信号（v0.7.0）。引擎的 `session/create` **不写库**：sqlite 里的
        # `session` 行是第一条 `session/send` 被处理时才插的（实测见 README
        # 「会话落盘时序」）。而 `v4/command sendText` 要往 `session_input` 插
        # 一行带 `references session(id)` 外键的记录 —— 抢在 session 行之前发，
        # 引擎必然回 `fault.command.executionFailed / FOREIGN KEY constraint failed`。
        # 本会话的第一条 `session/event` 稳定晚于 session 行 100~120ms，
        # 是协议面上可用的"已落盘"确定信号。
        self._persisted = threading.Event()
        self.on_stop = None        # 取消时：结算已挂在客户端的权限请求
        self.on_cancel_tasks = None  # 取消时：best-effort 停掉子代理后台任务
        self.child_lock = threading.RLock()   # V4 帧(读者线程) / 轮询(主线程) 共改子会话状态
        self.lock = threading.Lock()
        self.event_cb = None    # session/event 推送(读者线程回调)
        self._reader = threading.Thread(target=self._read_loop, daemon=True)
        self._reader.start()
        self._responder = threading.Thread(target=self._autoanswer_loop, daemon=True)
        self._responder.start()

    def _send(self, obj):
        # ⚠️ 读线程(自动应答引擎请求)与主线程(发请求)并发写同一根 stdin
        # 管道，没锁时 JSON 行会交错，引擎收到坏输入 → 请求静默丢失
        # （曾导致 workspace-config 订阅收不到帧、create 偶发超时）。
        with self._stdin_lock:
            self.proc.stdin.write((json.dumps(obj) + "\n").encode())
            self.proc.stdin.flush()

    def _read_loop(self):
        for line in self.proc.stdout:
            try:
                msg = json.loads(line)
            except Exception:
                continue
            cb = None
            with self.lock:
                self.log.append(msg)
            if msg.get("method") == "session/event":
                # 本会话的第一条事件 = 引擎已经把 session 行写进库（见
                # __init__ 里 _persisted 的注释）。这里在读者线程最早的位置
                # 置位，steer() 靠它决定什么时候可以安全地发 v4 sendText。
                if (msg.get("params") or {}).get("sessionId") == self.session_id:
                    self._persisted.set()
            if msg.get("method") == "session/event" and self.event_cb:
                cb = self.event_cb
            elif msg.get("method") == "v4/conversation/frame" and self.v4_cb:
                self.last_v4_frame_ts = time.time()
                cb = self.v4_cb
            elif msg.get("method") == "v4/conversation/frame":
                if os.environ.get("ZCODE_ACP_DEBUG_V4"):
                    log(f"v4 frame: topic={str((msg.get('params') or {}).get('topic'))[:60]} "
                        f"wc_cb={bool(self.v4_wc_cb)} turn_cb={bool(self.v4_cb)} "
                        f"inst={id(self)%10000}")
                if self.v4_wc_cb and not self.v4_cb:
                    cb = self._on_workspace_config_frame
            elif "id" in msg and msg.get("id") in self.v4_pending:
                topic = self.v4_pending.pop(msg["id"])
                sub = self.v4_subs.get(topic)
                if sub is not None:
                    if isinstance(msg.get("result"), dict):
                        ack = msg["result"].get("ack") or {}
                        sub.update(ack, pending=False)
                        log(f"v4 subscribed {topic[:40]} sub={ack.get('subscriptionId')} "
                            f"rows={(ack.get('openTiming') or {}).get('snapshotRowCount')}")
                    else:
                        self.v4_subs.pop(topic, None)
                        self.v4_topic_owner.pop(topic, None)
                        log(f"v4 subscribe failed: {topic} "
                            f"{json.dumps(msg.get('error'), ensure_ascii=False)[:200]}")
            if cb:
                try:
                    cb(msg.get("params") or {})
                except Exception as e:
                    log(f"event dispatch failed: {e}")

    def _autoanswer_loop(self):
        answered = set()
        pending_decisions = {}   # requestId -> None(决策中)|decision
        latest_mid = {}          # requestId -> 最新公告的 server 请求 id（应答必须回最新）
        while True:
            with self.lock:
                snapshot = list(self.log)
            for m in snapshot:
                mid = m.get("id")
                method = m.get("method")
                if method and mid is not None and "server" in str(mid):
                    key = f"{mid}:{method}"
                    if key in answered:
                        continue
                    answered.add(key)
                    if method == "session/requestRuntimePreferences":
                        self._send({"id": mid, "result": {"nativeSearchEnhancementsEnabled": False}})
                    elif method == "interaction/requestOfficialMcpAuthHeaders":
                        self._send({"id": mid, "result": {"headersApplied": False}})
                    elif method == "interaction/requestPermission":
                        # 重发公告（reannounceIntervalMs=1s）共用同一 requestId：
                        # 决策一次，应答永远回最新公告的 server id
                        rid = m["params"].get("requestId")
                        latest_mid[rid] = mid
                        if rid not in pending_decisions:
                            pending_decisions[rid] = None
                            threading.Thread(target=self._decide_permission,
                                             args=(m["params"], pending_decisions, rid),
                                             daemon=True).start()
                    elif method == "interaction/requestUserInput":
                        # 询问用户（AskUserQuestion 形状）→ ui_cb 转发 ACP
                        # elicitation/create。线程内等待,不堵自动应答循环。
                        threading.Thread(target=self._decide_user_input,
                                         args=(dict(m["params"]), mid),
                                         daemon=True).start()
                    else:
                        self._send({"id": mid, "result": {}})
            for rid, d in list(pending_decisions.items()):
                mid = latest_mid.get(rid)
                if d and mid is not None:
                    self.answer_permission(mid, d)
                    pending_decisions.pop(rid, None)
                    latest_mid.pop(rid, None)
            time.sleep(0.3)

    def _decide_user_input(self, params, mid):
        """询问用户：ui_cb(params) → 引擎应答 {action, content} / {action:"decline"}。"""
        cb = getattr(self, "ui_cb", None)
        if not cb:
            # 无通道可问：明确拒绝（不能回 {} ——zod 会拒收导致引擎重试风暴）
            self._send({"id": mid, "result": {"action": "decline"}})
            return
        try:
            answer = cb(params)   # {"action":"accept","content":{...}} | {"action":"decline"}
        except Exception as e:
            log(f"user-input forward failed: {e}")
            answer = {"action": "decline"}
        self._send({"id": mid, "result": answer or {"action": "decline"}})

    def engine_call(self, method, params, timeout=15):
        """一次性引擎请求，返回 result（失败/超时返回 None）。"""
        r = self.engine_call_raw(method, params, timeout=timeout)
        return r.get("result") if isinstance(r, dict) and isinstance(r.get("result"), dict) else None

    def engine_call_raw(self, method, params, timeout=15):
        """同上，但返回完整应答消息（含 error，便于诊断）。"""
        rid = self.next_id; self.next_id += 1
        self._send({"id": rid, "method": method, "params": params})
        deadline = time.time() + timeout
        while time.time() < deadline:
            with self.lock:
                r = next((m for m in reversed(self.log) if m.get("id") == rid), None)
            if r is not None:
                return r
            time.sleep(0.2)
        return None

    def _decide_permission(self, params, pending_decisions, rid):
        tool_name = params.get("toolName", "tool")
        tname = (tool_name or "").lower()
        mode = getattr(self, "permission_mode", "default")
        decision = None
        # 安全模式（客户端的模式选择器）：
        #   完全访问 → 全部放行；计划模式 → 只读（编辑/执行类直接拒绝）；
        #   自动编辑 → 编辑类自动放行，其余照问；变更前确认 → 全部照问。
        if mode == "bypassPermissions":
            decision = "allow"
        elif mode == "plan" and tname not in READ_TOOLS:
            decision = "deny"
        elif mode == "acceptEdits" and (tname in EDIT_TOOLS or tname in READ_TOOLS):
            decision = "allow"
        if decision is not None:
            pending_decisions[rid] = decision
            log(f"decision {rid} {tool_name} -> {decision} (mode={mode})")
            return
        # ⚠️ turn 已结束（用户点了终止 / 轮已收尾）时，引擎侧的子代理/工具
        # 仍可能继续发权限请求。若照旧转发，客户端会一直弹授权框——实测报障的
        # "点终止后还一直弹框"就是这个。所以：无活动轮 → 直接拒绝。
        if not getattr(self, "turn_active", False):
            decision = "deny"
            log(f"decision {rid} {tool_name} -> deny (no active turn)")
        elif getattr(self, "permission_cb", None):
            # ACP 模式：把 ZCode 的权限请求原样转发为 ACP
            # session/request_permission，等客户端的审批决策（行内卡片）。
            # 超时 600s 自动拒绝。
            try:
                decision = self.permission_cb(params) or "deny"
            except Exception as e:
                log(f"permission forward failed: {e}")
                decision = "deny"
        else:
            decision = "allow"
        # 二次确认：等客户端回复的这段时间里轮可能已被终止 —— 一律改判 deny。
        # （之前就栽在这：结算挂起框回的是 cancelled，下面误判成 allow，
        #  引擎收到 allow → 子代理继续跑 → 继续要权限 → 无限弹框。）
        if not getattr(self, "turn_active", False):
            if decision == "allow":
                log(f"decision {rid} {tool_name} -> allow overturned to deny "
                    f"(turn ended while waiting)")
            decision = "deny"
        pending_decisions[rid] = decision
        log(f"decision {rid} {tool_name} -> {decision}")

    def create_session(self):
        # 新建的会话还没落盘（引擎只在首条 session/send 时插 session 行）
        self._persisted.clear()
        key = "acp-" + uuid.uuid4().hex[:8]
        self.workspace = {"workspacePath": self.cwd, "workspaceKey": key}
        params = {"workspace": self.workspace}
        if self.mcp_servers:
            # 客户端 MCP（客户端自带 + 用户配置）原样透传给引擎；
            # 引擎把它当 per-load runtime 配置（不随会话持久化）→ resume 也要重挂。
            params["mcpServers"] = self.mcp_servers
            log("session/create 透传 " + str(len(self.mcp_servers)) + " 个客户端 MCP: "
                + ", ".join(str(s.get("name")) for s in self.mcp_servers
                            if isinstance(s, dict)))
        rid = self.next_id; self.next_id += 1
        self._send({"id": rid, "method": "session/create", "params": params})
        t0 = time.time()
        deadline = t0 + 60
        while time.time() < deadline:
            with self.lock:
                for m in self.log:
                    if m.get("method") == "session/requestRuntimePreferences" and \
                            m.get("params", {}).get("scope") == "runtime-materialization":
                        self.session_id = m["params"]["sessionId"]
                        break
                hit = any(m.get("id") == rid and isinstance(m.get("result"), dict) for m in self.log)
                err = next((m for m in self.log
                            if m.get("id") == rid and "error" in m), None)
            if err is not None:
                # 引擎明确拒绝（如 mcpServers 形状不符）→ 快失败并带上原因，
                # 别盲等 60s 丢错误信息（客户端侧只会看到笼统的 create failed）
                why = json.dumps(err.get("error"), ensure_ascii=False)
                log(f"session/create 被引擎拒绝: {why[:500]}")
                raise RuntimeError(f"zcode session/create rejected: {why[:300]}")
            if self.session_id and hit:
                log(f"session/create ok in {time.time()-t0:.1f}s")
                break
            time.sleep(0.5)
        if not self.session_id:
            log(f"session/create 超时 {time.time()-t0:.1f}s（引擎未应答；"
                f"引擎 stderr 见 {ENGINE_LOG_PATH}）")
            raise RuntimeError("zcode session/create failed")
        # 目录真值快照：usage 圆环的 size 从这里取（见 MODEL_AVAIL_LIST 注释）。
        with self.lock:
            created = next((m for m in reversed(self.log)
                            if m.get("id") == rid and isinstance(m.get("result"), dict)),
                           None)
        capture_availability((created or {}).get("result"))
        # 订阅实时事件流(桌面版同款通道):part.delta / model.streaming /
        # tool.updated / turn.completed … 全部从这里推送
        srid = self.next_id; self.next_id += 1
        self._send({"id": srid, "method": "session/subscribe",
                    "params": {"sessionId": self.session_id,
                               "deliveryKind": "desktop-continuous",
                               "includeSnapshot": False}})
        deadline = time.time() + 15
        while time.time() < deadline:
            with self.lock:
                hit = any(m.get("id") == srid and isinstance(m.get("result"), dict)
                          for m in self.log)
            if hit:
                break
            time.sleep(0.3)
        if MODEL:
            mid = self.next_id; self.next_id += 1
            prov, _, model = MODEL.partition("/")
            self._send({"id": mid, "method": "session/setModel",
                        "params": {"sessionId": self.session_id,
                                   "model": {"providerId": prov, "modelId": model}}})
        return self.session_id

    def _read_messages(self, timeout=6):
        """一次 session/read,返回 result(失败返回 None)。"""
        rid = self.next_id; self.next_id += 1
        self._send({"id": rid, "method": "session/read",
                    "params": {"sessionId": self.session_id}})
        deadline = time.time() + timeout
        while time.time() < deadline:
            with self.lock:
                resp = next((m for m in reversed(self.log) if m.get("id") == rid), None)
            if resp is not None and ("result" in resp or "error" in resp):
                return resp.get("result") if isinstance(resp.get("result"), dict) else None
            time.sleep(0.2)
        return None

    def _snapshot_message_ids(self):
        """轮次起点基线:当前会话已有消息 id。基线内的历史消息永不转发
        ——这就是"上一答案秒出"的根治。"""
        r = self._read_messages()
        ids = set()
        for msg in (r or {}).get("messages") or []:
            mid = (msg.get("info") or {}).get("messageId")
            if mid:
                ids.add(mid)
        return ids

    def send(self, text, on_update=None, timeout=1800, usage_cb=None,
             tool_cb=None, thought_cb=None, subagent_cb=None):
        """事件驱动流式一轮。

        实时通道(session/subscribe 推送,桌面版同款):
          model.streaming kind=reasoning_delta → thought_cb(思考逐 token)
          model.streaming kind=text_delta      → on_update(正文逐 token)
          model.streaming kind=tool_call       → 输入封口
          tool.updated scheduled/started/result → tool_cb 工具卡生命周期
          turn.completed/failed                → 本轮结束
          子会话(session/subagents 发现并订阅) → subagent_cb 全量转发
        兜底通道(每 2s session/read + 基线过滤):补工具卡、补漏段;
        历史消息(基线内)一律跳过,不重放。
        返回:本轮最终正文文本。"""
        baseline = self._snapshot_message_ids()
        st = {
            "baseline": baseline,
            "tools": {},         # toolCallId -> {"name","status","announced"}
            "text_emitted": {},  # assistantMessageId -> 已流出的正文字符数
            "thought_emitted": {},
            "children": {},      # 引擎子会话 id -> {"spawned","done","meta","title"}
            "agent_inputs": {},  # 父会话 Agent 工具 callId -> 派发入参(description/prompt)
            "done": threading.Event(),
            "failed": None,
            "stopped": False,
        }
        self._turn = st

        def note_agent_input(tid, name, inp):
            """父会话里 Agent/Task 工具的派发入参（description/prompt/subagent_type）
            就是子代理的任务原文——留给 spawn 卡用，比问子会话更直接。"""
            if tid and isinstance(inp, dict) and (name or "").lower() in ("agent", "task"):
                st["agent_inputs"][tid] = inp

        def on_event(params):
            sid = params.get("sessionId")
            if sid and sid != self.session_id:
                # 子会话(或其他会话)的推送 → 子代理路由
                if subagent_cb and sid in st["children"]:
                    self._child_event(st, params, subagent_cb)
                return
            et = params.get("type")
            pl = params.get("payload") or {}
            if et == "model.streaming":
                k = pl.get("kind")
                amid = pl.get("assistantMessageId")
                if k in ("reasoning_start", "reasoning_delta"):
                    if thought_cb and pl.get("delta"):
                        thought_cb(pl["delta"])
                    if amid is not None:
                        st["thought_emitted"][amid] = \
                            st["thought_emitted"].get(amid, 0) + len(pl.get("delta") or "")
                elif k == "text_delta":
                    if on_update and pl.get("delta"):
                        on_update(pl["delta"])
                    if amid is not None:
                        st["text_emitted"][amid] = \
                            st["text_emitted"].get(amid, 0) + len(pl.get("delta") or "")
                elif k in ("tool_input_start", "tool_input_delta", "tool_input_end"):
                    # 工具输入是流式 JSON 串,逐段累积
                    tid = pl.get("toolCallId")
                    if tid:
                        cur = st["tools"].setdefault(
                            tid, {"name": pl.get("toolName") or "tool",
                                  "status": "pending"})
                        if pl.get("toolName"):
                            cur["name"] = pl["toolName"]
                        if k == "tool_input_start":
                            cur["input_str"] = pl.get("delta") or ""
                        elif k == "tool_input_delta":
                            cur["input_str"] = (cur.get("input_str") or "") + \
                                                (pl.get("delta") or "")
                        else:
                            try:
                                cur["input"] = json.loads(cur.get("input_str") or "")
                            except Exception:
                                cur["input"] = None
                            note_agent_input(tid, cur["name"], cur["input"])
                        # 已发过首卡且现在输入就绪 → refine(标题/rawInput 修正)
                        if k == "tool_input_end" and cur.get("announced") \
                                and cur.get("input") is not None:
                            tool_cb(tid, {"name": cur["name"], "phase": "refine",
                                          "input": cur["input"]})
                elif k == "tool_call":
                    tid = pl.get("toolCallId")
                    if tid:
                        cur = st["tools"].setdefault(
                            tid, {"name": pl.get("toolName") or "tool",
                                  "status": "pending"})
                        cur["name"] = pl.get("toolName") or cur["name"]
                        if not cur.get("input") and cur.get("input_str"):
                            try:
                                cur["input"] = json.loads(cur["input_str"])
                            except Exception:
                                pass
                        if pl.get("input") is not None:
                            cur["input"] = pl["input"]
                        note_agent_input(tid, cur["name"], cur.get("input"))
            elif et == "tool.updated":
                k = pl.get("kind")
                tid = pl.get("toolCallId")
                if not tid:
                    return
                # 子代理自己的工具(引擎在父流上打 source="subagent" + childSessionId)
                # → 归到子会话面板,不污染主时间线
                cid = pl.get("childSessionId") or _child_sid_from_tool_call(tid)
                if cid:
                    ch = self._note_child(st, cid, {
                        "agentId": pl.get("agentId"), "agentType": pl.get("agentType"),
                        "parentToolCallId": pl.get("parentToolCallId"),
                        "description": pl.get("description")})
                    if subagent_cb:
                        res = pl.get("result") or {}
                        kind = ("result" if k == "result" else
                                "started" if k == "started" else "scheduled")
                        for d in self._child_tool_apply(
                                ch, pl.get("childToolCallId") or tid,
                                pl.get("toolName"), kind,
                                input_=pl.get("input"),
                                ok=res.get("success", True),
                                output=str(res.get("content") or res.get("error") or "")):
                            subagent_cb("tool", cid, d)
                    return
                if not tool_cb:
                    return
                cur = st["tools"].setdefault(
                    tid, {"name": pl.get("toolName") or "tool", "status": "pending"})
                if pl.get("toolName"):
                    cur["name"] = pl["toolName"]
                # scheduled 事件自带完整 input(非 model_stream 省略时)
                if pl.get("input") is not None and cur.get("input") is None:
                    cur["input"] = pl["input"]
                if k in ("scheduled", "started", "tool_call"):
                    # 输入没就绪就先压住首卡(等 tool_input_end/tool_call 事件),
                    # 避免 title/rawInput 空白;started 仍未就绪则用已知信息发卡
                    ready = cur.get("input") is not None or \
                        (k != "scheduled" and not cur.get("input_str"))
                    if not cur.get("announced") and (ready or k == "started"):
                        cur["announced"] = True
                        tool_cb(tid, {"name": cur["name"],
                                      "phase": "scheduled" if cur.get("input") is not None
                                               or not cur.get("input_str") else "started",
                                      "input": cur.get("input"),
                                      "source": pl.get("source")})
                    elif not cur.get("announced"):
                        pass   # scheduled 且输入未流完 → 等 tool_input_end
                    new_status = "pending" if k == "scheduled" else "in_progress"
                    if cur["status"] != new_status:
                        cur["status"] = new_status
                        if cur.get("announced") and new_status == "in_progress":
                            tool_cb(tid, {"name": cur["name"], "phase": "started",
                                          "source": pl.get("source")})
                elif k == "result":
                    res = pl.get("result") or {}
                    ok = res.get("success", True)
                    out = str(res.get("content") or res.get("error") or "")
                    new_status = "completed" if ok else "failed"
                    if not cur.get("announced"):
                        cur["announced"] = True
                        tool_cb(tid, {"name": cur["name"], "phase": "scheduled",
                                      "input": pl.get("input"),
                                      "source": pl.get("source")})
                    if cur["status"] != new_status:
                        cur["status"] = new_status
                        tool_cb(tid, {"name": cur["name"], "phase": "result",
                                      "ok": ok, "output": out,
                                      "source": pl.get("source")})
            elif et == "turn.steerQueued":
                # 引擎受理了一条注入。payload.delivery == "guide" 说明它进了
                # guide 车道（本轮内注入）；没有 delivery 字段 = 普通排队。
                intent = pl.get("intent") or {}
                cid = intent.get("sourceCommandId") or pl.get("inputId")
                self._steer_note(cid, {
                    "admitted": (intent.get("admittedDelivery") or pl.get("delivery")
                                 or "queue"),
                    "requested": intent.get("requestedDelivery"),
                    "pendingInputId": pl.get("pendingInputId"),
                    "reasonCode": intent.get("fallbackReasonCode"),
                    "state": ("steering"
                              if (intent.get("admittedDelivery") or pl.get("delivery"))
                              == "guide" else "notRequested"),
                }, admitted=True)
            elif et == "turn.steerDrained":
                # 真正注入进了当前 turn 的消息历史 → state=guided。
                for item in pl.get("drainedInputs") or []:
                    it = item.get("intent") or {}
                    cid = it.get("sourceCommandId")
                    self._steer_note(cid, {
                        "admitted": item.get("delivery") or "queue",
                        "messageId": item.get("messageId"),
                        "state": ("guided" if item.get("delivery") == "guide"
                                  else "notRequested"),
                    }, admitted=True, settled=(item.get("delivery") == "guide"))
            elif et == "turn.completed":
                st["ended"] = True          # 自然结束 → 硬停兜底可跳过
                st["done"].set()
                self._steer_release()
            elif et == "turn.failed":
                st["failed"] = json.dumps(pl, ensure_ascii=False)[:300] or "turn failed"
                st["ended"] = True
                st["done"].set()
                self._steer_release()

        self.event_cb = on_event

        def on_frame(params):
            """V4 会话帧 → 路由。

            这是**桌面端同款**的实时通道：子会话的 reasoning/assistantText 以
            {"op":"row.delta","path":"text","append":"The"} 逐 token 下发，
            toolCall 行带完整 input / output.text。旧通道拿不到子会话的
            model.streaming，正是因为引擎把它分流给了 V4 网关。

            父 topic 的帧用来**事件驱动地发现子代理**（snapshot/deltas 里的
            subagents 段）——比 session/subagents 轮询快得多，短任务的思考
            不会漏。"""
            topic = params.get("topic")
            payload = (params.get("frame") or {}).get("payload") or {}
            if topic == self.v4_parent_topic:
                self._v4_parent_frame(st, payload, subagent_cb)
                return
            zid = self.v4_topic_owner.get(topic) if topic else None
            if not zid or not subagent_cb:
                return
            ch = st["children"].get(zid)
            if not ch or ch.get("attached") != "v4":
                return
            f = params.get("frame") or {}
            payload = f.get("payload") or {}
            if os.environ.get("ZCODE_ACP_DEBUG_V4"):
                log(f"v4 frame topic=…{str(topic)[-14:]} kind={payload.get('kind')} "
                    f"seq={f.get('fromSeq')}→{f.get('toSeq')} "
                    f"ops={[d.get('op') for d in (payload.get('deltas') or [])]}")
            for action, data in self._v4_apply_payload(ch, payload):
                subagent_cb(action, zid, data)

        self.v4_cb = on_frame
        # 回合一开始就订阅父会话 topic：子代理一 spawn 就能从父帧里看到，
        # 然后立刻订阅子会话 topic —— 这是把短任务思考流不漏掉的关键。
        try:
            self.v4_parent_topic = f"conversation/{self.session_id}"
            self.v4_subscribe(self.v4_parent_topic, owner=None)
        except Exception as e:
            log(f"v4 parent subscribe failed: {e}")
        try:
            send_rid = self.next_id; self.next_id += 1
            self._send({"id": send_rid, "method": "session/send",
                        "params": {"sessionId": self.session_id, "content": text}})
            deadline = time.time() + timeout
            last_status = None
            retried_model = False
            while time.time() < deadline:
                if st["done"].wait(2.0):
                    break
                # session/send 直接被拒(如 resume 后原模型未物化)→ 自愈重试
                with self.lock:
                    err = next((m for m in reversed(self.log)
                                if m.get("id") == send_rid and "error" in m), None)
                if err is not None:
                    errtxt = json.dumps(err.get("error", {}), ensure_ascii=False)
                    if "RUNTIME_MODEL_UNAVAILABLE" in errtxt and not retried_model:
                        retried_model = True
                        log(f"model unavailable after resume → recovering: {errtxt[:160]}")
                        self._recover_model()
                        send_rid = self.next_id; self.next_id += 1
                        self._send({"id": send_rid, "method": "session/send",
                                    "params": {"sessionId": self.session_id,
                                               "content": text}})
                        continue
                    raise RuntimeError(f"zcode send rejected: {errtxt[:300]}")
                status = self._poll_fill(st, tool_cb, usage_cb, subagent_cb)
                if status == "idle" and last_status == "running" and not st["done"].is_set():
                    # 事件流缺失时的兜底结束信号
                    st["done"].set()
                    break
                if status:
                    last_status = status
            # 主轮已结束,子代理必然已归终(引擎等子代理 join 后才收轮)。
            # 但子会话自己的 V4 尾帧可能比父轮 turn.completed 晚一点点到,
            # 先给它们一个短窗口,否则正文/思考的尾巴会被丢掉。
            if subagent_cb and st["children"]:
                deadline = time.time() + 2.5
                while time.time() < deadline:
                    time.sleep(0.2)
                    with self.lock:
                        last = self.last_v4_frame_ts or 0
                    if time.time() - last > 0.6:
                        break
            if subagent_cb:
                try:
                    self._sync_subagents(st, subagent_cb)
                except Exception as e:
                    log(f"final subagent sync failed: {e}")
                for zid, ch in st["children"].items():
                    self._child_flush(st, zid, subagent_cb)
                    ch["done"] = True
                    if not ch.get("spawned"):
                        continue
                    if not ch.get("state_sent"):
                        ch["state_sent"] = True
                        subagent_cb("state", zid, {"state": "completed"})
            # 收尾:补齐事件流可能漏掉的正文尾巴(基线过滤 → 不重放历史)
            final = self._final_text(st, on_update)
            if st["failed"] and not final:
                raise RuntimeError(f"zcode turn failed: {st['failed']}")
            return final
        finally:
            self.event_cb = None
            self.v4_cb = None
            self.v4_parent_topic = None

    # -------------------------------------------------- 子代理（v0.5.0）

    def _note_child(self, st, zid, meta=None):
        """登记子会话（幂等）。返回该子会话状态字典。"""
        ch = st["children"].get(zid)
        if ch is None:
            ch = st["children"][zid] = {
                "spawned": False, "done": False, "state_sent": False,
                "meta": {}, "title": "", "summary": "", "task": "",
                "activated": False, "activate_tried": 0.0, "activate_failed": 0,
                "parts": {}, "tools": {}, "stream": {},
                "agent_id": "", "agent_type": "", "parent_tool_call_id": "",
                "description": "", "summary_sent": False, "last_poll": 0.0,
                "backfilled": False,
            }
        if meta:
            for k in ("agentId", "agentType", "parentToolCallId", "description"):
                v = meta.get(k)
                if v and not ch.get(_META_KEY[k]):
                    ch[_META_KEY[k]] = v
            for k, v in (meta.get("meta") or {}).items():
                ch["meta"].setdefault(k, v)
            if ch["meta"].get("status") in ("success", "failed", "cancelled"):
                ch["done"] = True
            if ch["meta"].get("summary") and not ch["summary"]:
                ch["summary"] = ch["meta"]["summary"]
            t = ch["meta"].get("title")
            if t and not ch["title"]:
                ch["title"] = t
        return ch

    def _activate_child(self, st, zid):
        """激活子会话 = 引擎 `session/resume`。

        关键突破：子代理跑在父运行时内嵌进程里，app-server 侧的该会话默认
        "not active"（read/subscribe 全报 -32004）。但子会话的**持久化转录**
        一直在 store 里（引擎自己的 session/subagents 就是这么读的）。
        resume 一次即把它载入活动会话表 → read/messages/events 立刻可用，
        且**随子代理推进持续增长**（实测 6.5KB→15.4KB），里面 reasoning/text/tool
        部件齐全（input、output 都是全文）。这就是"子代理细节"的取数通道。"""
        ch = st["children"].get(zid)
        if not ch or ch.get("activated"):
            return ch
        now = time.time()
        if now - ch.get("activate_tried", 0) < 3.0:
            return ch
        if ch.get("activate_failed", 0) >= 4:
            ch["activated"] = -1        # 明确放弃：不再反复等超时
            return ch
        ch["activate_tried"] = now
        params = {"sessionId": zid}
        if self.workspace:
            params["workspace"] = self.workspace
        r = self.engine_call("session/resume", params, timeout=15)
        if r is None:
            ch["activate_failed"] = ch.get("activate_failed", 0) + 1
            if ch["activate_failed"] <= 3:
                log(f"child activate failed ({ch['activate_failed']}): {zid}")
            return ch
        ch["activated"] = True
        log(f"child activated: {zid}")
        return ch

    def _child_backfill(self, st, zid, subagent_cb):
        """回填/对账：用持久化转录把面板内容补齐。

        V4 是实时的，但我们发现子会话（session/subagents 轮询/父帧）天然晚
        1~2 秒，短任务里思考可能已经流完；而且正文尾帧可能晚于父轮结束。
        resume+read 拿全文，和 V4 帧共用同一本台账（键=通道:助手消息id），
        按已发长度对齐——幂等，可反复调用。"""
        ch = st["children"].get(zid)
        if not ch or not subagent_cb:
            return
        if ch.get("activated") is not True:
            self._activate_child(st, zid)
        if ch.get("activated") is not True:
            return
        r = self.child_read(zid, timeout=12)
        if not r:
            return
        ev = []
        for msg in r.get("messages") or []:
            info = msg.get("info") or {}
            if info.get("role") != "assistant":
                continue
            if (info.get("semantics") or {}).get("kind") != "assistant_response":
                continue
            eid = info.get("messageId")
            for part in msg.get("parts") or []:
                ptype = part.get("type")
                if ptype == "reasoning" and not part.get("ignored"):
                    ev += self._stream_full(ch, "thought", eid, part.get("text") or "")
                elif ptype == "text" and not part.get("ignored"):
                    ev += self._stream_full(ch, "text", eid, part.get("text") or "")
                elif ptype == "tool":
                    tstate = part.get("state") or {}
                    stt = tstate.get("status")
                    k = ("result" if stt in ("completed", "error")
                         else "started" if stt == "running" else "scheduled")
                    out = str(tstate.get("output") or tstate.get("error") or "")
                    for d in self._child_tool_apply(
                            ch, part.get("callId"), part.get("tool"), k,
                            input_=tstate.get("input"),
                            ok=(stt != "error"), output=out):
                        ev.append(("tool", d))
        for action, data in ev:
            subagent_cb(action, zid, data)

    def _child_peek_task(self, zid, limit=800):
        """激活后先取子代理拿到的任务原文(子会话的第一条 user 消息)。
        只读不记账——user 部件本来就不上面板,spawn 描述因此一次到位。"""
        r = self.child_read(zid, timeout=10)
        if not r:
            return ""
        for msg in r.get("messages") or []:
            if ((msg.get("info") or {}).get("role")) != "user":
                continue
            for part in msg.get("parts") or []:
                if part.get("type") == "text" and part.get("text"):
                    return part["text"].strip()[:limit]
        return ""

    def _child_tool_apply(self, ch, ctid, name, kind, input_=None, ok=True, output=None):
        """把一条子代理工具事件并入子会话登记表，返回要发的 ACP 事件。

        两条通道（父流上 source="subagent" 的 tool.updated 实时事件；V4 帧/子会话
        转录轮询）喂同一张登记表（键=子代理的 callId），所以**不会重复出卡**，
        且谁先到谁出首卡、另一条只做状态/结果的补充。"""
        out = []
        with self.child_lock:
            out = self._child_tool_apply_locked(ch, ctid, name, kind, input_, ok, output)
        return out

    def _child_tool_apply_locked(self, ch, ctid, name, kind, input_=None,
                                 ok=True, output=None):
        if not ctid:
            return []
        rec = ch["tools"].get(ctid)
        if rec is None:
            rec = ch["tools"][ctid] = {"name": name or "tool", "announced": False,
                                        "status": "", "out_len": 0, "input": None,
                                        "input_sent": False}
        if name:
            rec["name"] = name
        if input_ is not None and rec.get("input") is None:
            rec["input"] = input_
        out = []
        if not rec["announced"]:
            rec["announced"] = True
            rec["input_sent"] = rec.get("input") is not None
            out.append({"name": rec["name"], "tid": ctid, "phase": "scheduled",
                        "input": rec.get("input")})
        elif rec.get("input") is not None and not rec.get("input_sent"):
            # 首卡发出时输入还没到 → 补一次 rawInput 修正
            rec["input_sent"] = True
            out.append({"name": rec["name"], "tid": ctid, "phase": "refine",
                        "input": rec["input"]})
        if kind == "started":
            if rec["status"] != "in_progress":
                rec["status"] = "in_progress"
                out.append({"name": rec["name"], "tid": ctid, "phase": "started",
                            "input": rec.get("input")})
        elif kind == "result":
            newst = "completed" if ok else "failed"
            text = output or ""
            if rec["status"] != newst or rec.get("out_len", 0) != len(text):
                rec["status"] = newst
                rec["out_len"] = len(text)
                out.append({"name": rec["name"], "tid": ctid, "phase": "result",
                            "ok": ok, "output": text})
        return out

    # -------------------------------------------------- V4 通道（v0.6.0）

    def v4_connect(self):
        """建立 V4 连接：`v4/connection/flow {connectionId, state}`。

        这是桌面端用的那条通道。旧通道（session/subscribe → session/event）在
        分发时对「非本会话」的事件直接 return 给 V4 网关：

            if (String(r.sessionId) !== t.app.sessionId) {
                e.v4Gateway?.ingestDetachedLiveSession(String(r.sessionId), r);
                return;
            }

        所以父流上永远看不到子会话的 model.streaming——不是引擎不给，是**走了
        另一条管子**。V4 订阅按 topic=conversation/<sessionId> 找 publisher，
        注册表独立于 `e.sessions`（不需要 resume 激活）。"""
        if self.v4_conn:
            return self.v4_conn
        self.v4_conn = "conn-" + uuid.uuid4().hex[:8]
        rid = self.next_id; self.next_id += 1
        self._send({"id": rid, "method": "v4/connection/flow",
                    "params": {"connectionId": self.v4_conn, "state": "drained"}})
        return self.v4_conn

    def v4_subscribe(self, topic, owner=None, wait=False):
        """订阅 V4 topic（conversation/<sessionId>）。默认即发即忘——
        首帧由 postResponseOutbox 在响应之前推出，等 ack 反而会漏。

        ⚠️ owner 必须在请求发出**之前**登记好 topic 归属，否则首帧没人认领。"""
        if topic in self.v4_subs:
            return self.v4_subs[topic]
        cid = self.v4_connect()
        if owner is not None:
            self.v4_topic_owner[topic] = owner
        rid = self.next_id; self.next_id += 1
        self.v4_pending[rid] = topic
        self._send({"id": rid, "method": "v4/conversation/subscribe",
                    "params": {"topic": topic, "connectionId": cid,
                               "clientMode": "desktop-continuous"}})
        sub = {"subscriptionId": None, "pending": True}
        self.v4_subs[topic] = sub
        if not wait:
            return sub
        deadline = time.time() + 20
        while time.time() < deadline:
            with self.lock:
                r = next((m for m in reversed(self.log) if m.get("id") == rid), None)
            if r is not None:
                ack = ((r.get("result") or {}).get("ack")) or None
                if ack:
                    sub.update(ack, pending=False)
                    return sub
                self.v4_topic_owner.pop(topic, None)
                self.v4_subs.pop(topic, None)
                log(f"v4 subscribe failed: {topic} "
                    f"{json.dumps(r.get('error'), ensure_ascii=False)[:200]}")
                return None
            time.sleep(0.1)
        self.v4_topic_owner.pop(topic, None)
        self.v4_subs.pop(topic, None)
        log(f"v4 subscribe timeout: {topic}")
        return None

    @staticmethod
    def _v4_tool_phase(status):
        if status in ("success", "error", "cancelled"):
            return "result"
        if status in ("running", "pendingApproval"):
            return "started"
        return "scheduled"

    # -------------------------------------------------- 子会话文本台账（v0.6.0）
    #
    # 两条来源喂同一本台账，键 = (通道, 助手消息 id)，记「已发字符数」：
    #   · V4 行     entityId/assistantResponseId = 助手消息 id
    #   · 转录轮询  部件的 messageId = 助手消息 id
    # 这样 V4 负责实时、session/read 负责回填（订阅建立前错过的部分），
    # 谁先到谁发，另一边按长度对齐，不会重复也不会漏。

    def _stream_full(self, ch, kind, eid, text):
        """全文对账：text 比「已发」长就补差值。返回待发事件列表。"""
        if not eid or not text:
            return []
        with self.child_lock:
            led = ch["stream"].setdefault(f"{kind}:{eid}", {"sent": 0, "buf": ""})
            if led["buf"] and text.startswith(led["buf"]) and len(text) > len(led["buf"]):
                delta = text[len(led["buf"]):]
            elif len(text) > led["sent"]:
                delta = text[led["sent"]:]
            else:
                return []
            led["buf"] = text
            led["sent"] = max(led["sent"], len(text))
            return [(kind, {"text": delta})]

    def _stream_append(self, ch, kind, eid, append):
        """增量入账（V4 row.delta）。返回待发事件列表。"""
        if not eid or not append:
            return []
        with self.child_lock:
            led = ch["stream"].setdefault(f"{kind}:{eid}", {"sent": 0, "buf": ""})
            led["buf"] = (led["buf"] or "") + append
            if len(led["buf"]) > led["sent"]:
                delta = led["buf"][led["sent"]:]
                led["sent"] = len(led["buf"])
                return [(kind, {"text": delta})]
        return []

    def _v4_row_upsert(self, ch, row):
        """V4 会话行 → ACP 事件（行是桌面的投影模型，比 part 更贴近 UI）。

        行 kind：reasoning / assistantText / toolCall / turnHeader / userInput …
        toolCall 行带 toolName/status/input/output.text（完整入参回参）。"""
        if not isinstance(row, dict):
            return []
        rid = row.get("rowId")
        kind = row.get("kind")
        rec = ch["v4"]["rows"].setdefault(rid, {"kind": None, "pending": ""})
        if kind:
            rec["kind"] = kind
        if kind == "toolCall":
            tid = row.get("toolCallId") or row.get("entityId") or rid
            status = row.get("status") or ""
            out = ""
            if isinstance(row.get("output"), dict):
                out = row["output"].get("text") or ""
            if not out and isinstance(row.get("error"), dict):
                out = row["error"].get("message") or ""
            return [("tool", d) for d in self._child_tool_apply(
                ch, tid, row.get("toolName"), self._v4_tool_phase(status),
                input_=row.get("input"), ok=(status != "error"), output=out)]
        if kind in ("reasoning", "assistantText"):
            chan = "thought" if kind == "reasoning" else "text"
            eid = row.get("entityId") or row.get("assistantResponseId") \
                or f"row-{rid}"
            rec["eid"] = eid          # 之后的 row.delta 只有 rowId，要靠它找台账
            ev = []
            pend = rec.get("pending") or ""
            if pend:
                # 订阅建立前没有行类型可用 → 这里补入台账
                rec["pending"] = ""
                ev += self._stream_append(ch, chan, eid, pend)
            ev += self._stream_full(ch, chan, eid, row.get("text") or "")
            return ev
        return []

    def _v4_row_delta(self, ch, rid, path, append):
        """逐 token 增量：{"op":"row.delta","rowId":4,"path":"text","append":"The"}"""
        if path != "text" or not append:
            return []
        rec = ch["v4"]["rows"].setdefault(rid, {"kind": None, "pending": ""})
        kind = rec.get("kind")
        if kind == "reasoning" or kind == "assistantText":
            eid = rec.get("eid")
            if not eid:
                return []
            return self._stream_append(
                ch, "thought" if kind == "reasoning" else "text", eid, append)
        # 行类型还没到（appended 在初始快照里/尚未下发）→ 缓冲，等 upsert 补发
        rec["pending"] = (rec.get("pending") or "") + append
        return []

    def _v4_apply_payload(self, ch, payload):
        """一帧 payload → 事件列表。snapshot 给全量窗口，deltas 给增量。"""
        ev = []
        with self.child_lock:
            kind = (payload or {}).get("kind")
            if kind == "snapshot":
                snap = payload.get("snapshot") or {}
                for row in ((snap.get("rows") or {}).get("window") or []):
                    ev += self._v4_row_upsert(ch, row)
            elif kind == "deltas":
                for d in payload.get("deltas") or []:
                    if not isinstance(d, dict):
                        continue
                    op = d.get("op")
                    if op in ("row.appended", "row.upserted"):
                        ev += self._v4_row_upsert(ch, d.get("row") or {})
                    elif op == "row.delta":
                        ev += self._v4_row_delta(ch, d.get("rowId"),
                                                 d.get("path"), d.get("append"))
        return ev

    # -------------------------------------------------- steering（v0.7.0）

    def _steer_note(self, cid, fields, admitted=False, settled=False):
        """把一条投递裁决记进台账。读者线程调用，幂等。"""
        if not cid:
            return
        with self._steer_lock:
            rec = self._steer.get(cid)
            if rec is None:
                return
            for k, v in fields.items():
                if v is not None:
                    rec[k] = v
            if admitted:
                rec["admitted_ev"].set()
            if settled:
                rec["settled_ev"].set()

    def _steer_release(self):
        """轮结束 → 唤醒所有还在等裁决的 steering 请求（不再会有新信号）。"""
        with self._steer_lock:
            recs = list(self._steer.values())
        for rec in recs:
            rec["turn_over"] = True
            rec["admitted_ev"].set()
            rec["settled_ev"].set()

    def _v4_scan_queue(self, payload):
        """从 V4 父帧里读投递队列状态（snapshot 与 deltas 同构）。"""
        items = []
        kind = (payload or {}).get("kind")
        if kind == "snapshot":
            q = ((payload.get("snapshot") or {}).get("queue") or {})
            items += q.get("items") or []
        elif kind == "deltas":
            for d in payload.get("deltas") or []:
                if not isinstance(d, dict) or d.get("op") != "state.updated":
                    continue
                q = (d.get("patch") or {}).get("queue")
                if isinstance(q, dict):
                    items += q.get("items") or []
        for it in items:
            if not isinstance(it, dict):
                continue
            cid = it.get("sourceCommandId")
            delivery = it.get("delivery") or {}
            steer = it.get("steer") or {}
            state = steer.get("state")
            self._steer_note(cid, {
                "admitted": delivery.get("admitted"),
                "requested": delivery.get("requested"),
                "reasonCode": (steer.get("reasonCode")
                               or delivery.get("fallbackReasonCode")),
                "pendingInputId": it.get("queueItemId"),
                "state": state,
            },
                admitted=state in ("steering", "guided", "fellBack"),
                settled=state in ("guided", "fellBack"))

    def steer(self, text, settle_timeout=None):
        """把一条补充消息注入**正在跑的那一轮**（引擎原生 guide 投递）。

        协议（逆向 + 实测，见 README「引擎注入协议」）：

            v4/command {commandId, clientId, sessionId,
                        type:"sendText",
                        payload:{text, requestedDelivery:"guide"}, issuedAt}
              → {commandId, status:"accepted", revisionAtDecision,
                 result:{type:"inputAccepted", delivery:"queue", inputId}}

        ⚠️ 应答里的 `result.delivery` **不是**裁决结果（实测 guide 被受理时它
        照样是 "queue"），只能当"已受理"用。真裁决在事件/帧上：
        `turn.steerQueued.delivery=="guide"` → 进了 guide 车道；
        `turn.steerDrained` → 真的并进了当前 turn；
        V4 `queue.items[].steer.state=="fellBack"` + reasonCode → 引擎降级成排队。

        legacy `session/send` 做不到：schema（OEt）里根本没有 delivery 字段，
        轮次进行中发过去会被当普通排队（实测 steerQueued 无 delivery、
        steer.state=notRequested，本轮答案完全不受影响）。

        **落盘时序**（v0.7.0，实测见 README「会话落盘时序」）：`session/create`
        不写库，sqlite 的 `session` 行是首条 `session/send` 被处理时才插的
        （实测 send 后 222~310ms）；而本命令要往 `session_input` 插一行带
        `references session(id)` 外键的记录。抢在前面发 → 引擎回
        `fault.command.executionFailed / FOREIGN KEY constraint failed`。
        所以这里先等"已落盘"的确定信号（本会话第一条 `session/event`，
        实测比 session 行晚 100~120ms、比 send 晚 313~413ms），等不到再对
        FK 做有界重试；两道都不成立才回 failed 并透传引擎原话。

        返回 {"outcome": injected|queued|promptRequired|failed, ...}。"""
        if settle_timeout is None:
            settle_timeout = float(os.environ.get("ZCODE_ACP_STEER_SETTLE_MS", "8000")) / 1000.0
        persist_wait = float(os.environ.get("ZCODE_ACP_STEER_PERSIST_MS", "3000")) / 1000.0
        fk_budget = float(os.environ.get("ZCODE_ACP_STEER_FK_RETRY_MS", "2000")) / 1000.0
        st = getattr(self, "_turn", None)
        if not self.turn_active or not st or st["done"].is_set():
            return {"outcome": "promptRequired", "reason": "noRunningTurn"}
        # ① 确定信号：等本会话第一条 session/event（= session 行已入库）
        if not self._persisted.is_set():
            t_wait = time.time()
            got = self._persisted.wait(persist_wait)
            log(f"steer: waited {time.time()-t_wait:.3f}s for session persistence "
                f"signal (got={got})")
        cid = rec = None
        try:
            cid, rec, verdict = self._steer_admit(text, st, fk_budget)
            if verdict is not None:
                return verdict
            # 受理 ≠ 进了 guide 车道：等引擎的裁决信号
            rec["admitted_ev"].wait(10.0)
            if rec.get("turn_over") and rec.get("state") is None:
                return {"outcome": "promptRequired", "reason": "noRunningTurn"}
            if rec.get("admitted") == "guide" or rec.get("state") in ("steering", "guided"):
                # 已进 guide 车道 → 再给一小段时间看它是真落进本轮（guided）
                # 还是被引擎降级（fellBack，例如轮次被取消 guide.turnInterrupted）
                rec["settled_ev"].wait(max(0.0, settle_timeout))
                if rec.get("turn_over") and rec.get("state") != "guided":
                    # 轮已结束但没见到 steerDrained：引擎此时会发
                    # TurnSteerDeliveryChanged(fellBack)，它只走 V4 帧，可能
                    # 比 turn.completed 晚一点点 → 给一小段对账余量。
                    rec["settled_ev"].clear()
                    rec["settled_ev"].wait(1.5)
                    if rec.get("state") != "guided":
                        return {"outcome": "queued", "delivery": "queue",
                                "steerState": rec.get("state") or "steering",
                                "reasonCode": rec.get("reasonCode"),
                                "reason": "turnEndedBeforeDrain",
                                "pendingInputId": rec.get("pendingInputId")}
                if rec.get("state") == "fellBack":
                    return {"outcome": "queued", "delivery": "queue",
                            "steerState": "fellBack",
                            "reasonCode": rec.get("reasonCode"),
                            "pendingInputId": rec.get("pendingInputId")}
                return {"outcome": "injected", "delivery": "guide",
                        "steerState": rec.get("state") or "steering",
                        "pendingInputId": rec.get("pendingInputId")}
            if rec.get("state") == "fellBack" or rec.get("admitted") == "queue":
                return {"outcome": "queued", "delivery": "queue",
                        "steerState": rec.get("state") or "notRequested",
                        "reasonCode": rec.get("reasonCode"),
                        "pendingInputId": rec.get("pendingInputId")}
            return {"outcome": "failed", "reason": "noVerdict"}
        finally:
            if cid:
                with self._steer_lock:
                    self._steer.pop(cid, None)

    @staticmethod
    def _is_fk_fault(result):
        """引擎的"会话还没落盘"故障：reasonCode=fault.command.executionFailed +
        message 里带 sqlite 的外键报错原话。"""
        if not isinstance(result, dict):
            return False
        msg = str(result.get("message") or result.get("detail") or "")
        return ("FOREIGN KEY" in msg.upper()
                and result.get("reasonCode") in (None, "fault.command.executionFailed"))

    def _steer_command_once(self, cid, text, timeout=20):
        """发一条 v4 sendText(guide)，返回引擎应答报文（超时返回 None）。"""
        rid = self.next_id; self.next_id += 1
        self._send({"id": rid, "method": "v4/command", "params": {
            "commandId": cid, "clientId": "zcode-acp",
            "sessionId": self.session_id, "type": "sendText",
            "payload": {"text": text, "requestedDelivery": "guide"},
            "issuedAt": int(time.time() * 1000)}})
        deadline = time.time() + timeout
        while time.time() < deadline:
            with self.lock:
                resp = next((m for m in reversed(self.log) if m.get("id") == rid), None)
            if resp is not None:
                return resp
            time.sleep(0.02)
        return None

    def _steer_admit(self, text, st, fk_budget):
        """把一条 guide 投递送进引擎并拿到"已受理"。

        返回 (cid, rec, verdict)：verdict 非空表示到此为止（失败/无轮可注）。
        FK 故障（会话尚未落盘）在 `fk_budget` 内退避重试（100/200/400/800ms，
        每次换新的 commandId —— 引擎按 commandId 去重，复用会被当成同一条）。
        预算用尽仍 FK → 如实回 failed 并透传引擎的 reason/detail。"""
        delay = 0.1
        deadline = time.time() + fk_budget
        attempt = 0
        while True:
            attempt += 1
            cid = uuid.uuid4().hex
            rec = {"admitted_ev": threading.Event(), "settled_ev": threading.Event(),
                   "state": None, "admitted": None, "reasonCode": None,
                   "pendingInputId": None, "turn_over": False}
            with self._steer_lock:
                self._steer[cid] = rec
            resp = self._steer_command_once(cid, text)
            if resp is None:
                return cid, rec, {"outcome": "failed", "reason": "engineTimeout"}
            if "error" in resp:
                err = resp["error"] or {}
                fail = {"outcome": "failed", "reason": "engineError",
                        "detail": json.dumps(err, ensure_ascii=False)[:300]}
                if not self._is_fk_fault({"message": err.get("message"),
                                          "reasonCode": None}):
                    return cid, rec, fail
            else:
                result = resp.get("result") or {}
                if result.get("status") == "accepted":
                    if attempt > 1:
                        log(f"steer: accepted after {attempt} attempts "
                            f"(FK retry worked)")
                    return cid, rec, None
                fail = {"outcome": "failed",
                        "reason": result.get("reasonCode") or "notAccepted",
                        "detail": str(result.get("message") or "")[:300]}
                if not self._is_fk_fault(result):
                    return cid, rec, fail
            # ② FK：会话行还没进库。有界退避重试，不把它当最终态抛出去。
            with self._steer_lock:
                self._steer.pop(cid, None)
            if st.get("done").is_set():
                return None, None, {"outcome": "promptRequired",
                                    "reason": "noRunningTurn"}
            remaining = deadline - time.time()
            if remaining <= 0:
                log(f"steer: FOREIGN KEY after {attempt} attempts / "
                    f"{fk_budget:.1f}s budget → failed")
                fail["reason"] = fail.get("reason") or "fault.command.executionFailed"
                fail["attempts"] = attempt
                return None, None, fail
            log(f"steer: FOREIGN KEY (session not persisted yet), retry #{attempt} "
                f"in {min(delay, remaining):.2f}s")
            time.sleep(min(delay, remaining))
            delay = min(delay * 2, 0.8)

    def _v4_parent_frame(self, st, payload, subagent_cb):
        """父会话 V4 帧 → 事件驱动地发现子代理并接上。

        父帧的 snapshot / state.updated patch 里带 `subagents` 段
        （{childSessionIds, running[], ended{items[]}}），比 session/subagents
        轮询快 1~2 秒 —— 这 1~2 秒正是短任务思考流会不会漏的差别。

        同一批帧还带 `queue` 段（patch.queue.items[]），那是 steering 投递裁决
        的**唯一**如实来源：`steer.state ∈ notRequested|submitting|steering|
        guided|fellBack` + `delivery.{requested,admitted,fallbackReasonCode}`。
        引擎把 guide 降级成排队时只发 TurnSteerDeliveryChanged，而该事件在
        协议映射里落进 default 分支（"session.updated"），旧通道分辨不出来。"""
        self._v4_scan_queue(payload)
        if not subagent_cb:
            return
        found = []
        kind = (payload or {}).get("kind")
        if kind == "snapshot":
            found.append((payload.get("snapshot") or {}).get("subagents"))
        elif kind == "deltas":
            for d in payload.get("deltas") or []:
                if not isinstance(d, dict) or d.get("op") != "state.updated":
                    continue
                p = d.get("patch") or {}
                if isinstance(p.get("subagents"), dict):
                    found.append(p["subagents"])
        for sub in found:
            if not isinstance(sub, dict):
                continue
            for entry in sub.get("running") or []:
                self._adopt_child(st, entry, subagent_cb)
            for entry in ((sub.get("ended") or {}).get("items") or []):
                self._adopt_child(st, entry, subagent_cb)
            for cid in sub.get("childSessionIds") or []:
                self._adopt_child(st, {"childSessionId": cid}, subagent_cb)

    def _adopt_child(self, st, entry, subagent_cb):
        """从父帧拿到子会话 → 登记 → spawn → 立刻接 V4。幂等，读者线程安全。"""
        zid = entry.get("childSessionId") if isinstance(entry, dict) else entry
        if not zid:
            return
        meta = entry if isinstance(entry, dict) else {}
        spawn = None
        with self.child_lock:
            ch = self._note_child(st, zid, {
                "agentId": meta.get("agentId"),
                "agentType": meta.get("subagentType"),
                "parentToolCallId": meta.get("toolCallId"),
                "meta": meta})
            if meta.get("status") in ("success", "failed", "cancelled"):
                ch["done"] = True
            if not ch.get("spawned"):
                ch["spawned"] = True
                dispatch = (st.get("agent_inputs") or {}).get(
                    ch.get("parent_tool_call_id"))
                name = (ch.get("description") or meta.get("title")
                        or (dispatch or {}).get("description")
                        or meta.get("subagentType") or ch.get("title")
                        or f"子代理 {len(st['children'])}")
                task = (ch.get("task") or (dispatch or {}).get("prompt")
                        or meta.get("summary") or meta.get("title") or "")
                spawn = {"name": str(name)[:80], "task": str(task)[:1200],
                         "type": ch.get("agent_type") or ""}
        if spawn is not None:
            subagent_cb("spawn", zid, spawn)   # subagent_cb 内部自带日志
        self._attach_child(st, zid)
        if ch.get("done") and not ch.get("state_sent"):
            ch["state_sent"] = True
            self._child_flush(st, zid, subagent_cb)
            subagent_cb("state", zid, {"state": "completed"})

    def _attach_child(self, st, zid):
        """给子会话接上数据源：优先 V4（实时 token 级），失败退回 resume+转录轮询。"""
        ch = st["children"].get(zid)
        if not ch or ch.get("attached"):
            return ch
        # ⚠️ 先挂状态再订阅：初始 snapshot 帧由 postResponseOutbox 在响应**之前**
        # 推出，早于 subscribe 的 ack 到达；晚挂就会被 on_frame 丢掉。
        ch["attached"] = "v4"
        ch.setdefault("v4", {"rows": {}})
        ack = self.v4_subscribe(f"conversation/{zid}", zid)
        if ack:
            ch["v4"]["ack"] = ack
            log(f"child V4 attached: {zid} sub={ack.get('subscriptionId')}")
        else:
            ch["attached"] = "poll"
            ch["v4_failed"] = True
            log(f"child V4 unavailable → fallback poll: {zid}")
            self._activate_child(st, zid)
        return ch

    def _child_poll(self, st, zid, subagent_cb, force=False):
        """子会话 read → 增量转发到 zcsub-<id> 面板。

        按 partId 记账：reasoning/text 只发新增字符(逐段追加)，
        tool 部件按 status/output 变化发卡与结果。"""
        ch = st["children"].get(zid)
        if not ch or ch.get("attached") == "v4":
            return
        if ch.get("activated") is not True:
            return
        if not force and time.time() - ch.get("last_poll", 0) < 1.2:
            return
        ch["last_poll"] = time.time()
        r = self.child_read(zid)
        if not r:
            return
        ev = []
        for msg in r.get("messages") or []:
            info = msg.get("info") or {}
            role = info.get("role")
            kind = (info.get("semantics") or {}).get("kind")
            for part in msg.get("parts") or []:
                ptype = part.get("type")
                pid = part.get("partId") or f"{ptype}:{part.get('callId')}"
                if role == "user":
                    if ptype == "text" and not ch.get("task"):
                        ch["task"] = (part.get("text") or "").strip()
                    continue
                if kind != "assistant_response":
                    continue
                if ptype in ("reasoning", "text"):
                    if part.get("ignored"):
                        continue
                    txt = part.get("text") or ""
                    chan = "thought" if ptype == "reasoning" else "text"
                    ev += self._stream_full(ch, chan, pid, txt)
                elif ptype == "tool":
                    tstate = part.get("state") or {}
                    ctid = part.get("callId") or pid
                    stt = tstate.get("status")
                    kind = ("result" if stt in ("completed", "error")
                            else "started" if stt == "running" else "scheduled")
                    out = str(tstate.get("output") or tstate.get("error") or "")
                    for d in self._child_tool_apply(
                            ch, ctid, part.get("tool"), kind,
                            input_=tstate.get("input"),
                            ok=(stt != "error"), output=out):
                        ev.append(("tool", d))
        if subagent_cb:
            for action, data in ev:
                subagent_cb(action, zid, data)
        return ev

    def _sync_subagents(self, st, subagent_cb):
        """发现引擎子会话 → 激活(resume) → 订阅 → read 回填 → 通知 spawn。"""
        if not subagent_cb:
            return
        r = self.engine_call("session/subagents",
                             {"sessionId": self.session_id, "endedLimit": 20},
                             timeout=8)
        if not r:
            return
        for entry in r.get("running") or []:
            zid = entry.get("childSessionId") if isinstance(entry, dict) else entry
            if zid:
                self._note_child(st, zid, {"agentId": entry.get("agentId") if
                                            isinstance(entry, dict) else None,
                                            "agentType": entry.get("subagentType") if
                                            isinstance(entry, dict) else None,
                                            "parentToolCallId": entry.get("toolCallId") if
                                            isinstance(entry, dict) else None,
                                            "meta": entry if isinstance(entry, dict) else {}})
        for entry in ((r.get("ended") or {}).get("items")) or []:
            zid = entry.get("childSessionId") if isinstance(entry, dict) else entry
            if zid:
                self._note_child(st, zid, {"agentId": entry.get("agentId") if
                                            isinstance(entry, dict) else None,
                                            "agentType": entry.get("subagentType") if
                                            isinstance(entry, dict) else None,
                                            "parentToolCallId": entry.get("toolCallId") if
                                            isinstance(entry, dict) else None,
                                            "meta": entry if isinstance(entry, dict) else {}})
                st["children"][zid]["done"] = True
        for zid in r.get("childSessionIds") or []:
            self._note_child(st, zid)

        for zid, ch in list(st["children"].items()):
            with self.child_lock:
                if not ch.get("spawned"):
                    # spawn 必须先发：子会话内容(帧/轮询)要挂在已宣告的面板下
                    ch["spawned"] = True
                    meta = ch.get("meta") or {}
                    dispatch = (st.get("agent_inputs") or {}).get(
                        ch.get("parent_tool_call_id"))
                    name = (ch.get("description") or meta.get("title")
                            or (dispatch or {}).get("description")
                            or meta.get("subagentType") or ch.get("title")
                            or f"子代理 {len(st['children'])}")
                    task = (ch.get("task") or (dispatch or {}).get("prompt")
                            or meta.get("summary") or meta.get("title") or "")
                    spawn = {"name": str(name)[:80], "task": str(task)[:1200],
                             "type": ch.get("agent_type") or ""}
                    do_spawn = True
                else:
                    do_spawn = False
            if do_spawn:
                subagent_cb("spawn", zid, spawn)
            self._attach_child(st, zid)
            if ch.get("attached") == "v4":
                # 回填订阅前错过的部分（短任务里思考可能已经流完）
                if not ch.get("backfilled"):
                    self._child_backfill(st, zid, subagent_cb)
            else:
                if not ch.get("task") and ch.get("activated") is True:
                    t = self._child_peek_task(zid)
                    if t:
                        ch["task"] = t
                self._child_poll(st, zid, subagent_cb)
            if ch.get("done") and not ch.get("state_sent"):
                ch["state_sent"] = True
                self._child_flush(st, zid, subagent_cb)
                subagent_cb("state", zid, {"state": "completed"})

    def _child_flush(self, st, zid, subagent_cb):
        """子代理收尾：用持久化转录做最终对账（台账去重，不重不漏），
        V4 通道下正文/思考的尾巴在这里兜住。"""
        ch = st["children"].get(zid)
        if not ch or not subagent_cb:
            return
        # V4 是实时的，但发现子会话天然有延迟 → 轮末再对账一次，
        # 把订阅建立前/尾帧未推的部分补齐。resume+read 代价很小。
        self._child_backfill(st, zid, subagent_cb)
        summary = (ch.get("summary") or "").strip()
        if summary and not ch.get("summary_sent"):
            # 只有当面板正文还什么都没有时，才用引擎的 summary 兜底
            has_text = any(k.startswith("text:") for k in ch["stream"])
            ch["summary_sent"] = True
            if not has_text:
                subagent_cb("text", zid, {"text": summary})

    def child_read(self, zsid, timeout=12):
        """读任意引擎会话会话转录（子会话需先 resume 激活）。"""
        rid = self.next_id; self.next_id += 1
        self._send({"id": rid, "method": "session/read",
                    "params": {"sessionId": zsid}})
        deadline = time.time() + timeout
        while time.time() < deadline:
            with self.lock:
                r = next((m for m in reversed(self.log) if m.get("id") == rid), None)
            if r is not None and isinstance(r.get("result"), dict):
                return r["result"]
            time.sleep(0.15)
        return None

    def _engine_subscribe(self, zsid):
        """订阅任意引擎会话(子代理面板的数据源)。"""
        return self.engine_call("session/subscribe",
                                {"sessionId": zsid,
                                 "deliveryKind": "desktop-continuous",
                                 "includeSnapshot": False}, timeout=10)

    def _child_event(self, st, params, subagent_cb):
        """子会话推送 → subagent_cb 转发(子面板全量流:思考/正文/工具/状态)。"""
        zid = params.get("sessionId")
        ch = st["children"].get(zid)
        if not ch:
            return
        et = params.get("type")
        pl = params.get("payload") or {}
        if et == "model.streaming":
            k = pl.get("kind")
            if k in ("reasoning_start", "reasoning_delta") and pl.get("delta"):
                subagent_cb("thought", zid, {"text": pl["delta"]})
            elif k == "text_delta" and pl.get("delta"):
                subagent_cb("text", zid, {"text": pl["delta"]})
            elif k in ("tool_input_start", "tool_input_delta", "tool_input_end"):
                cur = ch.setdefault("tools", {}).setdefault(
                    pl.get("toolCallId") or "?", {})
                if pl.get("toolName"):
                    cur["name"] = pl["toolName"]
                if k == "tool_input_start":
                    cur["input_str"] = pl.get("delta") or ""
                elif k == "tool_input_delta":
                    cur["input_str"] = (cur.get("input_str") or "") + (pl.get("delta") or "")
                else:
                    try:
                        cur["input"] = json.loads(cur.get("input_str") or "")
                    except Exception:
                        cur["input"] = None
                    subagent_cb("tool", zid, {"name": cur.get("name") or "tool",
                                               "phase": "scheduled",
                                               "input": cur.get("input"),
                                               "tid": pl.get("toolCallId")})
        elif et == "tool.updated":
            k = pl.get("kind")
            tid = pl.get("toolCallId")
            if not tid:
                return
            cur = ch.setdefault("tools", {}).setdefault(tid, {"name": "tool"})
            if pl.get("toolName"):
                cur["name"] = pl["toolName"]
            if k in ("scheduled", "started", "tool_call"):
                subagent_cb("tool", zid, {"name": cur["name"],
                                           "phase": "started" if k == "started" else "scheduled",
                                           "input": pl.get("input"), "tid": tid})
            elif k == "result":
                res = pl.get("result") or {}
                subagent_cb("tool", zid, {"name": cur["name"], "phase": "result",
                                           "ok": res.get("success", True),
                                           "output": str(res.get("content") or ""),
                                           "tid": tid})
        elif et == "session.titleUpdated":
            t = pl.get("title")
            if t:
                ch["title"] = t
        elif et == "turn.completed":
            if not ch.get("done"):
                ch["done"] = True
                ch["state_sent"] = True
                subagent_cb("state", zid, {"state": "completed"})
        elif et == "turn.failed":
            ch["done"] = True
            ch["state_sent"] = True
            subagent_cb("state", zid, {"state": "failed"})

    def _recover_model(self):
        """resume 后原模型不可用时:切到 headless 配置的默认模型(已物化)。"""
        try:
            cfg = json.load(open(os.path.expanduser("~/.zcode/cli/config.json")))
            model = cfg.get("model") or ""
            pid, _, mid = model.partition("/")
            if pid and mid:
                rid = self.next_id; self.next_id += 1
                self._send({"id": rid, "method": "session/setModel",
                            "params": {"sessionId": self.session_id,
                                       "model": {"providerId": pid, "modelId": mid}}})
                time.sleep(1.5)
                return True
        except Exception as e:
            log(f"model recover failed: {e}")
        return False

    def _poll_fill(self, st, tool_cb, usage_cb, subagent_cb=None):
        """兜底轮询:只处理基线之外的消息。正文/思考由事件通道负责,
        这里只补工具卡(事件缺失时)、同步/读取子代理、回报 projection 状态。"""
        if subagent_cb:
            self._sync_subagents(st, subagent_cb)
        r = self._read_messages()
        if not r:
            return None
        if usage_cb:
            u = self._fetch_usage()
            if u:
                usage_cb(u)
        for msg in r.get("messages") or []:
            info = msg.get("info", {})
            mid = info.get("messageId")
            if not mid or mid in st["baseline"]:
                continue
            if info.get("semantics", {}).get("kind") != "assistant_response":
                continue
            for part in msg.get("parts", []):
                if part.get("type") != "tool" or not tool_cb:
                    continue
                tid = part.get("callId") or part.get("partId")
                state = part.get("state") or {}
                cur = st["tools"].setdefault(
                    tid, {"name": part.get("tool") or "tool", "status": "pending"})
                cur["name"] = part.get("tool") or cur["name"]
                s = state.get("status")
                if s in (None, "pending", "running") and not cur.get("announced"):
                    cur["announced"] = True
                    if s == "running":
                        cur["status"] = "in_progress"
                    tool_cb(tid, {"name": cur["name"],
                                  "phase": "started" if s == "running" else "scheduled",
                                  "input": state.get("input")})
                elif s == "running" and cur["status"] == "pending":
                    cur["status"] = "in_progress"
                    tool_cb(tid, {"name": cur["name"], "phase": "started"})
                new_status = {"completed": "completed", "error": "failed"}.get(s)
                if new_status and cur["status"] != new_status:
                    if not cur.get("announced"):
                        # 轮询直接见到终态：先补 pending 卡再发结果
                        cur["announced"] = True
                        tool_cb(tid, {"name": cur["name"], "phase": "scheduled",
                                      "input": state.get("input")})
                    cur["status"] = new_status
                    out = str(state.get("output") or state.get("error") or "")
                    tool_cb(tid, {"name": cur["name"], "phase": "result",
                                  "ok": new_status == "completed", "output": out})
        return ((r.get("projection") or {}).get("status"))

    def _final_text(self, st, on_update):
        """轮末终读:返回值=本轮完整正文;事件已流出的部分只补尾巴,
        事件完全缺席的消息全文补发。基线过滤保证不重放历史轮。

        0.25s 只是读取余量：turn.completed 发出前引擎已把本轮消息落盘
        （事件/落盘走同一有序管道），原来的 1s 是纯延迟——cancel→settle
        快慢直接受它影响。"""
        time.sleep(0.25)
        r = self._read_messages()
        if not r:
            return ""
        full_parts = []
        for msg in r.get("messages") or []:
            info = msg.get("info", {})
            mid = info.get("messageId")
            if not mid or mid in st["baseline"]:
                continue
            if info.get("semantics", {}).get("kind") != "assistant_response":
                continue
            best = ""
            for part in msg.get("parts", []):
                if part.get("type") == "text":
                    t = part.get("text") or ""
                    if len(t) > len(best):
                        best = t
            if not best:
                continue
            full_parts.append(best)
            emitted = st["text_emitted"].get(mid, 0)
            if emitted < len(best) and on_update:
                on_update(best[emitted:])   # 补漏尾巴(通常为空)
                st["text_emitted"][mid] = len(best)
        return "\n".join(full_parts).strip()

    def _send_stop(self):
        """桌面端停止按钮的原生路径：v4/command type=stop。

        桌面端停止按钮走的就是这条通道（bundle v3.11.2 逐字节确认：
        bootstrap.zcode_protocol_v4.commands 的 stop→aji→
        runtime.stopActiveForegroundExecution，含队列 auto-drain 处理）。
        而协议里的 session/stop 只 abort 当前那次模型请求，中断的流还会被
        stream recovery 接上继续生成——所以这里**只发 v4 原生命令**。
        payload 留空 = 目标为当前前台执行（expectedForegroundExecutionId
        省略时不触发 stopTargetChanged 守卫，引擎空闲时是安全 no-op）。
        引擎若版本过旧不认这条命令 → 请求被忽略，兜底交给 _hard_stop_later。"""
        rid = self.next_id; self.next_id += 1
        self._send({"id": rid, "method": "v4/command", "params": {
            "commandId": uuid.uuid4().hex,
            "clientId": "zcode-acp",
            "sessionId": self.session_id,
            "type": "stop",
            "payload": {},
            "issuedAt": int(time.time() * 1000)}})

    def _subscribe_engine(self, zsid):
        """引擎级 session/subscribe（close+resume 之后必须重挂，否则事件流断了）。"""
        rid = self.next_id; self.next_id += 1
        self._send({"id": rid, "method": "session/subscribe",
                    "params": {"sessionId": zsid, "deliveryKind": "desktop-continuous",
                               "includeSnapshot": False}})
        deadline = time.time() + 15
        while time.time() < deadline:
            with self.lock:
                hit = any(m.get("id") == rid and isinstance(m.get("result"), dict)
                          for m in self.log)
            if hit:
                return True
            time.sleep(0.2)
        return False

    def subscribe_workspace_config(self, workspace_key, on_commands=None):
        """订阅 V4 workspace-config topic → 拿 ZCode 原生斜杠命令列表。

        桌面端的 `/` 菜单数据源就是这个 topic（快照 config.slashCommands，
        内置 + 用户自定义，带 description/inputHint）。拿到后回调 on_commands，
        由 ACP 层转成 available_commands_update 推给客户端。"""
        cid = self.v4_connect()
        topic = f"workspace-config/{workspace_key}"
        self.v4_wc_topic = topic
        # ⚠️ 回调必须先挂再发请求：初始快照帧由 postResponseOutbox 在 ack 之前推出
        self.v4_wc_cb = on_commands
        rid = self.next_id; self.next_id += 1
        self._send({"id": rid, "method": "v4/conversation/subscribe",
                    "params": {"topic": topic, "connectionId": cid,
                               "clientMode": "desktop-continuous"}})
        log(f"workspace-config subscribing: {topic} wc_cb={bool(self.v4_wc_cb)} "
            f"inst={id(self)%10000}")
        deadline = time.time() + 20
        resp = None
        while time.time() < deadline:
            with self.lock:
                resp = next((m for m in self.log if m.get("id") == rid), None)
                parsed = bool(self.slash_commands)
            if resp is not None or parsed:
                break
            time.sleep(0.2)
        if resp is None and parsed:
            return True
        if resp is None:
            log("workspace-config subscribe timeout")
            return False
        if isinstance(resp.get("error"), dict):
            log(f"workspace-config subscribe error: "
                f"{json.dumps(resp['error'], ensure_ascii=False)[:200]}")
            return False
        log(f"workspace-config ack: "
            f"{json.dumps((resp.get('result') or {}), ensure_ascii=False)[:160]}")
        return True

    def _on_workspace_config_frame(self, params):
        """workspace-config 帧 → 更新原生斜杠命令列表并通知 ACP 层。"""
        log("workspace-config frame received")
        payload = (params.get("frame") or {}).get("payload") or {}
        cfg = None
        kind = payload.get("kind")
        if kind == "snapshot":
            cfg = (payload.get("snapshot") or {}).get("config") or {}
        elif kind == "deltas":
            for d in payload.get("deltas") or []:
                if isinstance(d, dict) and d.get("op") == "config.updated":
                    cfg = d.get("config") or {}
        if not isinstance(cfg, dict):
            return
        cmds = cfg.get("slashCommands") or []
        self.slash_commands = [
            {"name": str(c.get("name") or "").lstrip("/"),
             "description": str(c.get("description") or ""),
             "inputHint": str(c.get("inputHint") or "")}
            for c in cmds if isinstance(c, dict) and c.get("name")]
        log(f"slash commands: {len(self.slash_commands)} 个 "
            f"({', '.join('/' + c['name'] for c in self.slash_commands)})")
        cb = self.v4_wc_cb
        if cb:
            try:
                cb(self.slash_commands)
            except Exception as e:
                log(f"on_commands callback failed: {e}")

    def hard_stop(self):
        """真·硬停（cancel 的兜底，实测必要）。

        引擎的 `session/stop` 只 abort **当前那次模型请求**（实测连普通轮都停不
        下来——abort 后引擎的 stream recovery 会把流接上继续生成），turn 里如果
        挂着子代理工具就更停不下来：turn.completed 一直不发、模型调用继续。

        `session/close` 是唯一能连根拔的：实测模型输出立刻归零、会话从活动表
        移除。而会话有持久化 → close 后 `session/resume` 用**同一个 sessionId**
        原地复活，历史不丢 —— 客户端侧完全无感。"""
        # ① close 前先把还在跑的子代理杀掉（close 之后就查不到了）
        r = self.engine_call("session/subagents",
                             {"sessionId": self.session_id, "endedLimit": 20},
                             timeout=8) or {}
        for e in (r.get("running") or []):
            aid = e.get("agentId")
            if aid:
                self.engine_call("session/cancelBackgroundTask",
                                 {"sessionId": self.session_id, "taskId": aid},
                                 timeout=8)
        # ② 硬停
        rr = self.engine_call("session/close", {"sessionId": self.session_id},
                              timeout=25)
        closed = bool(rr and rr.get("closed"))
        log(f"hard_stop: session/close -> {closed}")
        # ③ 原地复活（同一个 sessionId，客户端无感）
        ws = dict(self.workspace or {"workspacePath": self.cwd,
                                     "workspaceKey": "acp-" + uuid.uuid4().hex[:8]})
        registry = _v2_registry()
        if registry:
            self.engine_call("workspace/updateProviderRegistry",
                             {"workspace": dict(ws), "registry": registry},
                             timeout=30)
        rp = {"sessionId": self.session_id, "workspace": ws}
        if self.mcp_servers:
            rp["mcpServers"] = self.mcp_servers   # per-load 配置：复活时必须重挂
        r2 = self.engine_call("session/resume", rp, timeout=40)
        ok = r2 is not None
        if ok:
            self._subscribe_engine(self.session_id)
        log(f"hard_stop: session/resume -> {'ok' if ok else 'FAIL'}")
        # ④ 唤醒还在等 turn.completed 的 prompt 循环（close 不会再发它了）
        st = getattr(self, "_turn", None)
        if st:
            st["stopped"] = True
            st["done"].set()
        self.turn_active = False
        return ok

    def _hard_stop_later(self):
        """延迟硬停兜底：给引擎自己收尾一个窗口。

        v4 stop 生效后 turn.completed 通常 0.1~3s 内到（实测：流式中
        0.02s、模型请求前的准备阶段 ~2.6s）——期间每 0.5s 醒来看一眼，
        正常结束就安静退出；只有真的卡住（子代理工具在跑 / stream
        recovery 把流接回来，turn.completed 一直不来）才 close+resume。
        原来的固定 2.5s 盲等会跟引擎自己的收尾赛跑：close+resume 要 4s+，
        结果把 2.6s 能结束的轮拖成 8s+。"""
        deadline = time.time() + 6.0
        while time.time() < deadline:
            time.sleep(0.5)
            if getattr(self, "turn_active", False):
                log("hard_stop skipped (new prompt started)")
                return
            st = getattr(self, "_turn", None)
            if st is None:
                # 空闲 cancel（从未跑过 turn）：没有东西可停，别去 close+resume
                # —— 没跑过 turn 的会话还没持久化，close 后 resume 必然 -32004。
                log("hard_stop skipped (no turn yet)")
                return
            if st.get("ended") or st["done"].is_set():
                log("hard_stop skipped (turn ended)")
                return
        try:
            self.hard_stop()
        except Exception as e:
            log(f"hard_stop failed: {e}")

    def stop(self):
        """取消：①温和 stop（一次，不多发——实测每次 abort 都会触发引擎的
        stream recovery，多发反而让它继续烧 token）②结算已挂起的授权框
        ③turn_active=False → 后续权限请求一律直接拒绝 ④延迟硬停兜底。"""
        st = getattr(self, "_turn", None)
        if st:
            st["stopped"] = True
        self.turn_active = False
        self._send_stop()
        cb = getattr(self, "on_stop", None)
        if cb:
            try:
                cb()
            except Exception as e:
                log(f"on_stop callback failed: {e}")
        threading.Thread(target=self._hard_stop_later, daemon=True).start()

    def _current_model_ref(self):
        """日志里最新的"当前模型"引用 (providerId, modelId)。

        来源优先级：state.updated 的 patch.model.current（换模型后即到）、
        其次 create/read 结果的 settings.model.current。"""
        with self.lock:
            for m in reversed(self.log):
                params = m.get("params") if isinstance(m.get("params"), dict) else {}
                patch = params.get("patch") if isinstance(params.get("patch"), dict) else {}
                model = patch.get("model") if isinstance(patch.get("model"), dict) else {}
                cur = model.get("current") if isinstance(model.get("current"), dict) else {}
                if not (cur.get("providerId") and cur.get("modelId")):
                    result = m.get("result") if isinstance(m.get("result"), dict) else {}
                    settings = result.get("settings") if isinstance(result.get("settings"), dict) else {}
                    rmodel = settings.get("model") if isinstance(settings.get("model"), dict) else {}
                    cur = rmodel.get("current") if isinstance(rmodel.get("current"), dict) else {}
                if cur.get("providerId") and cur.get("modelId"):
                    return cur["providerId"], cur["modelId"]
        return None

    def _latest_usage_detail(self):
        """引擎的用量明细（cache 命中统计 + breakdown 占用分类）→ (cache, breakdown)。

        来源优先级（实测结论）：
        - breakdown：**ModelComplete 事件**（session/event 的 session.updated，
          payload.contextUsageBreakdown = [{source, chars}]）——这是唯一可靠来源：
          session/read 的 runtime.contextUsage 里它会被引擎的合并守卫吞掉
          （投影 size 与事件 contextWindow 不一致时不合并）。
        - cache：事件 payload.cacheHit 与 read 的 runtime.contextUsage.cache
          同 schema，取最新出现的即可。
        两者独立取"最新一次出现"，都没有则返回 (None, None)。"""
        cache = None
        breakdown = None
        with self.lock:
            for m in reversed(self.log):
                if m.get("method") == "session/event":
                    params = m.get("params") if isinstance(m.get("params"), dict) else {}
                    payload = params.get("payload") if isinstance(params.get("payload"), dict) else {}
                    if breakdown is None:
                        bd = payload.get("contextUsageBreakdown")
                        if isinstance(bd, list) and bd:
                            breakdown = bd
                    if cache is None:
                        ch = payload.get("cacheHit")
                        if isinstance(ch, dict) and ch:
                            cache = ch
                else:
                    res = m.get("result")
                    if isinstance(res, dict):
                        runtime = res.get("runtime") if isinstance(res.get("runtime"), dict) else {}
                        cu = runtime.get("contextUsage") if isinstance(runtime.get("contextUsage"), dict) else {}
                        if cache is None:
                            c = cu.get("cache")
                            if isinstance(c, dict) and c:
                                cache = c
                        if breakdown is None:
                            bd = cu.get("breakdown")
                            if isinstance(bd, list) and bd:
                                breakdown = bd
                if cache is not None and breakdown is not None:
                    break
        return cache, breakdown

    def _fetch_usage(self):
        """session/usage + 模型 contextWindow → usage_update 载荷（客户端上下文圆环）。"""
        rid = self.next_id; self.next_id += 1
        self._send({"id": rid, "method": "session/usage",
                    "params": {"sessionId": self.session_id}})
        deadline = time.time() + 4
        while time.time() < deadline:
            with self.lock:
                r = next((m for m in reversed(self.log) if m.get("id") == rid), None)
            if r is not None and isinstance(r.get("result"), dict):
                u = r["result"]
                size = None
                # 1) 目录真值优先（见 MODEL_AVAIL_LIST 注释：引擎投影的
                #    contextWindow 换模型不更新，会一直停在 2e5 默认值）。
                cur_ref = self._current_model_ref()
                if cur_ref:
                    size = catalog_context_window(*cur_ref)
                # 2) 旧来源兜底：patch.model.available / session/read projection。
                if size is None:
                    with self.lock:
                        for m in self.log:
                            patch = (m.get("params") or {}).get("patch") or {}
                            model = (patch.get("model") or {})
                            cur = model.get("current") or {}
                            if cur:
                                for a in (model.get("available") or []):
                                    if a.get("ref") == cur:
                                        size = a.get("contextWindow")
                            cw = (m.get("result", {}) or {}).get("projection", {}).get("contextWindow") \
                                if isinstance(m.get("result"), dict) else None
                            if size is None and cw:
                                size = cw
                # 3) 什么都没有时保留原来的 1M 兜底。
                used = u.get("totalTokens", 0)
                if not size:
                    size = 1000000
                payload = {"used": used, "size": size,
                           "cost": {"amount": 0, "currency": "USD"}}
                # 引擎明细随载荷带回（ACP 没有标准字段 → _meta.zcode，见 _send_usage）。
                cache, breakdown = self._latest_usage_detail()
                if cache:
                    payload["cache"] = cache
                if breakdown:
                    payload["breakdown"] = breakdown
                return payload
            time.sleep(0.3)
        return None

    def answer_permission(self, request_id, decision):
        self._send({"id": request_id, "result": {"decision": decision}})



# ---------------------------------------------------------------- ACP server

class AcpServer:
    def __init__(self):
        self.zcode = None
        self.acp_session = None
        self.permission_mode = "default"   # 变更前确认 | 自动编辑 | 计划模式 | 完全访问
        self.acp_permission_waiters = {}
        self._out_lock = threading.Lock()   # 读者线程(事件)与 handle 线程共写 stdout
        self._prompt_lock = threading.Lock()  # 轮次串行:引擎拒绝 active 期间二次 send
        self._last_usage_meta = None       # (sid, meta_json)：usage 明细去重哨兵

    def _deny_all_pending(self):
        """把所有还挂在客户端的权限请求按"取消"结算 —— 终止后弹着的框要收掉。"""
        with self._out_lock:
            waiters = list(self.acp_permission_waiters.items())
        if not waiters:
            return
        log(f"cancelling {len(waiters)} pending permission request(s)")
        for perm_id, q in waiters:
            try:
                q.put({"outcome": {"outcome": "cancelled"}})
            except Exception:
                pass

    def _cancel_running_subagents(self):
        """best-effort:把还在跑的子代理按后台任务取消（引擎有
        session/cancelBackgroundTask）。失败不打扰主流程。"""
        zc = self.zcode
        if not zc or not zc.session_id:
            return
        r = zc.engine_call("session/subagents",
                           {"sessionId": zc.session_id, "endedLimit": 20}, timeout=8) or {}
        tasks = []
        for e in (r.get("running") or []):
            if isinstance(e, dict) and e.get("agentId"):
                tasks.append(e["agentId"])
        for tid in tasks:
            rr = zc.engine_call("session/cancelBackgroundTask",
                                {"sessionId": zc.session_id, "taskId": tid}, timeout=10)
            log(f"cancelBackgroundTask {tid[:24]} -> "
                f"{'ok' if rr is not None else 'unsupported/failed'}")

    def _send(self, obj):
        with self._out_lock:
            sys.stdout.write(json.dumps(obj) + "\n")
            sys.stdout.flush()

    def handle(self, msg):
        method = msg.get("method")
        rid = msg.get("id")
        try:
            if method:
                trace(f"IN  {method} {json.dumps(msg.get('params') or {}, ensure_ascii=False)[:400]}")
            elif rid is not None:
                trace(f"RES id={rid} {json.dumps(msg.get('result') or msg.get('error') or {}, ensure_ascii=False)[:200]}")
        except Exception:
            pass
        if method == "initialize":
            self._send({"jsonrpc": "2.0", "id": rid, "result": {
                "protocolVersion": 1,
                "agentCapabilities": {"loadSession": False,
                                       "promptCapabilities": {"embeddedContext": False}},
                "sessionCapabilities": {"list": {}, "resume": {}, "fork": {}, "close": {}},
                "authMethods": [],
                # steering 扩展广告（顶层 _meta，与 claude-agent-acp / codex-acp
                # 同位置）。supported 之外的字段是本适配器实测出来的投递事实，
                # 供编排层直接生成 capability descriptor，见 README。
                # 只广告真的实现了的方法：session/goal 能力走 `/goal` 斜杠命令
                # （见 _run_slash_command），协议上没有 `_session/goal` 这个方法。
                "_meta": {"steering": {"supported": True,
                                        "method": "_session/steering",
                                        "tier": "extension",
                                        "boundary": "step",
                                        "idle": "promptRequired",
                                        "settlesOwnerTurn": False,
                                        "outcomes": list(STEERING_OUTCOMES)}}}})
        elif method == "session/new":
            new_params = msg.get("params", {})
            cwd = new_params.get("cwd") or os.getcwd()
            try:
                self.catalog = zcode_model_catalog(self.permission_mode)
                sync_channels_to_headless(self.catalog)
                self.zcode = ZCode(cwd)
                mcp_servers = new_params.get("mcpServers")
                if isinstance(mcp_servers, list) and mcp_servers:
                    # IN 行截断在 400 字符，MCP 全量单独落 trace（排查用）
                    trace("MCP FULL " + json.dumps(mcp_servers, ensure_ascii=False)[:4000])
                    self.zcode.mcp_servers = engine_mcp_servers(mcp_servers)
                zsid = self.zcode.create_session()
            except Exception as e:
                self._send({"jsonrpc": "2.0", "id": rid, "error":
                            {"code": -32000, "message": f"zcode session/new failed: {e}"}})
                return
            self.zcode.permission_cb = self._on_permission
            self.zcode.ui_cb = self._on_user_input
            self.acp_session = "zc-" + zsid
            log(f"session ready: zcode={zsid} acp={self.acp_session}")
            cat = zcode_model_catalog(self.permission_mode)
            # 先把原生斜杠命令取到手（最多等几秒），让它们直接进 session/new 应答
            try:
                wkey0 = (self.zcode.workspace or {}).get("workspaceKey")
                if wkey0:
                    self.zcode.subscribe_workspace_config(
                        wkey0, on_commands=self._push_slash_commands)
            except Exception as e:
                log(f"slash command prefetch failed: {e}")
            result = {"sessionId": self.acp_session,
                      "models": {"availableModels": cat["availableModels"],
                                  "currentModelId": cat["currentModelId"]},
                      "configOptions": cat["configOptions"],  # 顶层：客户端后端读的位置
                      "modes": {"currentModeId": self.permission_mode,
                                "availableModes": ACP_MODES},
                      "availableCommands": self._acp_commands()}
            self._send({"jsonrpc": "2.0", "id": rid, "result": result})
            # 当前模式也要主动推一次：前端快照的 currentModeId 靠 current_mode_update
            self._send({"jsonrpc": "2.0", "method": "session/update", "params": {
                "sessionId": self.acp_session, "update": {
                    "sessionUpdate": "current_mode_update",
                    "currentModeId": self.permission_mode}}})
            # 斜杠命令已在上面预取；这里只做延迟补推（防首帧没被接住）
            threading.Timer(3.0, self._repush_commands).start()


        elif method == "session/prompt":
            if not self.zcode or not self.zcode.session_id:
                self._send({"jsonrpc": "2.0", "id": rid, "error":
                            {"code": -32000, "message": "no active session"}})
                return
            text = "".join(c.get("text", "") for c in msg.get("params", {}).get("prompt", [])
                           if c.get("type") == "text")
            sid_acp = msg["params"]["sessionId"]
            # 斜杠命令 → ZCode 原生方法（/compact /goal /plan /init；
            # 自定义命令透传给引擎）。原生命令不走模型，直接回结果。
            if text.startswith("/"):
                try:
                    action, out = self._run_slash_command(text)
                except Exception as e:
                    log(f"slash command failed: {e}")
                    action, out = "handled", f"/命令执行失败：{e}"
                if action == "handled":
                    self._send({"jsonrpc": "2.0", "method": "session/update",
                                "params": {"sessionId": sid_acp, "update": {
                                    "sessionUpdate": "agent_message_chunk",
                                    "content": {"type": "text", "text": out}}}})
                    self._send({"jsonrpc": "2.0", "id": rid,
                                "result": {"stopReason": "end_turn"}})
                    return
            # 轮次进行中的并发 prompt → 走引擎 guide 投递注入当前轮
            # （不再降级排队）。语义见 README「并发 prompt 的收口语义」：
            # 注入成功后这条 prompt 请求成为当前轮的搭车者，等当前轮结束时
            # 以该轮的 stopReason 回执，并在 _meta.steering 里如实标注投递档位。
            got = self._prompt_lock.acquire(timeout=0.5)
            if not got:
                res = self.zcode.steer(text)
                if res.get("outcome") in ("injected", "queued"):
                    log(f"concurrent prompt → steering {res.get('outcome')} "
                        f"({res.get('steerState')})")
                    st = getattr(self.zcode, "_turn", None)
                    if st is not None:
                        st["done"].wait(1800)
                    stop = "cancelled" if (st or {}).get("stopped") else "end_turn"
                    self._send({"jsonrpc": "2.0", "id": rid, "result": {
                        "stopReason": stop, "_meta": {"steering": res}}})
                    return
                log(f"concurrent prompt not steerable ({res.get('outcome')}) "
                    f"→ 顺序执行")
                self._prompt_lock.acquire()
            try:
                self._run_prompt(rid, sid_acp, text)
            finally:
                self._prompt_lock.release()
        elif method == "_session/steering":
            self._handle_steering(rid, msg.get("params") or {})
        elif method == "session/set_mode":
            # 会话安全模式：同步到引擎原生 session/setMode（plan/build/edit/yolo），
            # 并推送 current_mode_update 让客户端选择器显示当前档位。
            pm = msg.get("params") or {}
            # 客户端发的是 modeId（引擎侧 SetSessionModeRequest 的字段），
            # 之前我们读 "mode" → 拿到 None → 一律回落 default，切换永远不生效
            new_mode = pm.get("modeId") or pm.get("mode") or "default"
            if new_mode not in MODE_TO_ENGINE:
                new_mode = "default"
            self._set_mode_internal(new_mode)
            self._send({"jsonrpc": "2.0", "id": rid, "result": {"modes": {
                "currentModeId": new_mode, "availableModes": ACP_MODES}}})
        elif method in ("session/set_config_option", "session/set_model"):
            # 用户在客户端的模型/推理选择器切换 → 同步到 ZCode 会话
            if not self.zcode or not self.zcode.session_id:
                self._send({"jsonrpc": "2.0", "id": rid, "error":
                            {"code": -32000, "message": "no active session"}})
                return
            p = msg["params"]
            config_id = (p.get("configId") or p.get("configOptionId")
                         or p.get("id") or p.get("optionId")
                         or ("model" if "modelId" in p else ""))
            if config_id not in ("mode", "reasoning_effort", "model"):
                log(f"set_config_option 未识别: {json.dumps(p, ensure_ascii=False)[:300]}")
            value = str(p.get("value") or p.get("modelId") or "")
            if config_id == "mode":
                # 客户端因有 configOptions 而隐藏 legacy 模式选择器，"安全模式"
                # 走的就是这条配置项通道 → 桥到引擎 session/setMode。
                if value not in MODE_TO_ENGINE:
                    self._send({"jsonrpc": "2.0", "id": rid, "error":
                                {"code": -32602, "message": f"unknown mode {value}"}})
                    return
                self._set_mode_internal(value)
                self._send({"jsonrpc": "2.0", "id": rid,
                            "result": {"configOptions": self._catalog_opts()}})
                return
            if config_id == "reasoning_effort":
                zrid = self.zcode.next_id; self.zcode.next_id += 1
                self.zcode._send({"id": zrid, "method": "session/setThoughtLevel",
                                  "params": {"sessionId": self.zcode.session_id,
                                             "thoughtLevel": value}})
                # 严格解析的客户端要求应答里带 configOptions 字段
                # （missing field `configOptions`）
                self._send({"jsonrpc": "2.0", "id": rid,
                            "result": {"configOptions": self._catalog_opts()}})
                return
            if config_id != "model":
                self._send({"jsonrpc": "2.0", "id": rid, "error":
                            {"code": -32602, "message": f"unknown configId {config_id}"}})
                return
            # 渠道名/模型 value → (providerId, modelId) 反查
            alias = (getattr(self, "catalog", {}) or {}).get("alias") or {}
            resolved = alias.get(value)
            if resolved:
                prov, model, _ = resolved
            elif "/" in value:
                prov, _, model = value.partition("/")
            else:
                self._send({"jsonrpc": "2.0", "id": rid, "error":
                            {"code": -32602, "message": "modelId must be provider/model"}})
                return
            zrid = self.zcode.next_id; self.zcode.next_id += 1
            self.zcode._send({"id": zrid, "method": "session/setModel",
                              "params": {"sessionId": self.zcode.session_id,
                                         "model": {"providerId": prov, "modelId": model}}})
            deadline = time.time() + 30
            ok = False
            while time.time() < deadline:
                with self.zcode.lock:
                    r = next((m for m in self.zcode.log if m.get("id") == zrid), None)
                if r is not None:
                    ok = "result" in r
                    if not ok:
                        self._send({"jsonrpc": "2.0", "id": rid, "error":
                                    {"code": -32000, "message": json.dumps(r.get("error", {}))[:200]}})
                        return
                    break
                time.sleep(0.3)
            opts = self._catalog_opts()
            self._send({"jsonrpc": "2.0", "id": rid, "result": {"configOptions": opts}})
            # 引擎侧模型已变 → 推送 config_option_update 让客户端选择器同步
            if ok and getattr(self, "catalog", None):
                try:
                    for opt in self.catalog["configOptions"]:
                        if opt.get("id") == "model":
                            opt["currentValue"] = value
                            break
                except Exception:
                    pass
                self._send({"jsonrpc": "2.0", "method": "session/update", "params": {
                    "sessionId": self.acp_session, "update": {
                        "sessionUpdate": "config_option_update",
                        "configOptions": self.catalog["configOptions"]}}})
        elif method == "session/list":
            sessions = self._list_sessions(msg.get("params") or {})
            self._send({"jsonrpc": "2.0", "id": rid, "result": {"sessions": sessions}})
        elif method == "session/resume":
            out = self._resume_session(msg.get("params") or {})
            if "error" in out:
                self._send({"jsonrpc": "2.0", "id": rid, "error":
                            {"code": -32000, "message": out["error"]}})
            else:
                self._send({"jsonrpc": "2.0", "id": rid, "result": out})
        elif method == "session/fork":
            out = self._fork_session(msg.get("params") or {})
            if "error" in out:
                self._send({"jsonrpc": "2.0", "id": rid, "error":
                            {"code": -32000, "message": out["error"]}})
            else:
                self._send({"jsonrpc": "2.0", "id": rid, "result": out})
        elif method == "session/close":
            out = self._close_session(msg.get("params") or {})
            if "error" in out:
                self._send({"jsonrpc": "2.0", "id": rid, "error":
                            {"code": -32000, "message": out["error"]}})
            else:
                self._send({"jsonrpc": "2.0", "id": rid, "result": {}})
        elif method == "session/cancel":
            # 取消：①结算已挂在客户端的权限框（不再弹）②turn_active=False
            # ③温和 stop ④延迟硬停兜底（session/close + resume，见 ZCode.hard_stop）
            if self.zcode and self.zcode.session_id:
                log("cancel → stop + settle pending permissions + hard-stop fallback")
                self.zcode.on_stop = self._deny_all_pending
                self.zcode.stop()
        elif rid is not None and "method" not in msg:
            # ACP 客户端对 session/request_permission 的应答
            waiter = self.acp_permission_waiters.get(str(rid))
            if waiter is not None:
                waiter.put(msg)
            # 否则是别的响应，忽略
        else:
            if rid is not None:
                self._send({"jsonrpc": "2.0", "id": rid, "error":
                            {"code": -32601, "message": f"method not found: {method}"}})

    @staticmethod
    def _steering_text(blocks):
        """ContentBlock[] → 引擎 sendText 的纯文本。

        引擎的 guide 投递只吃文本：带 attachments 的 sendText 会被引擎在受理
        时就降级成排队（`fallbackReasonCode: "guide.attachmentsUnsupported"`，
        bundle 里 `v==="guide"&&x.length>0` 那一支）。本适配器的
        promptCapabilities 也没有声明 image/embeddedContext，所以这里只取
        text 块——guide 车道上永远不会出现 attachments。"""
        if not isinstance(blocks, list):
            return ""
        return "".join(b.get("text", "") for b in blocks
                       if isinstance(b, dict) and b.get("type") == "text")

    def _handle_steering(self, rid, p):
        """`_session/steering` —— ACP steering 扩展（契约对齐 claude-agent-acp
        与 codex-acp 两份官方实现）。

        · 请求 {sessionId, prompt: ContentBlock[], _meta:{steering:{idleBehavior}}}
        · 轮次进行中 → 引擎 v4 guide 投递 → {"outcome":"injected"}
          （引擎把 guide 降级成排队时如实回 {"outcome":"queued"} + reasonCode）
        · 空闲 + idleBehavior="promptRequired" → {"outcome":"promptRequired",
          "reason":"noRunningTurn"}（内容留给客户端自己用 session/prompt 发）
        · 空闲 + 未声明 idleBehavior → 两份官方实现的既有默认：起一个分离的
          新回合，回 {"outcome":"startedNewTurn"}
        """
        def fail(code, message):
            self._send({"jsonrpc": "2.0", "id": rid,
                        "error": {"code": code, "message": message}})

        sid_acp = p.get("sessionId")
        if not isinstance(sid_acp, str) or not sid_acp:
            return fail(-32602, "steering params require a non-empty sessionId")
        blocks = p.get("prompt")
        if not isinstance(blocks, list) or not blocks:
            return fail(-32602, "steering params require a non-empty prompt array")
        idle_behavior = ((p.get("_meta") or {}).get("steering") or {}).get("idleBehavior")
        if idle_behavior is not None and idle_behavior != "promptRequired":
            return fail(-32602, "unsupported steering idleBehavior")
        if not self.zcode or not self.zcode.session_id:
            return fail(-32000, "no active session")
        text = self._steering_text(blocks)
        if not text.strip():
            return fail(-32602, "steering prompt must contain text content")

        res = self.zcode.steer(text)
        outcome = res.get("outcome")
        if outcome == "promptRequired":
            if idle_behavior == "promptRequired":
                self._send({"jsonrpc": "2.0", "id": rid, "result": {
                    "outcome": "promptRequired", "reason": "noRunningTurn"}})
                return
            threading.Thread(target=self._detached_turn, args=(sid_acp, text),
                             daemon=True).start()
            self._send({"jsonrpc": "2.0", "id": rid,
                        "result": {"outcome": "startedNewTurn"}})
            return
        if outcome == "failed":
            self._send({"jsonrpc": "2.0", "id": rid, "result": {
                "outcome": "failed",
                "_meta": {"steering": {k: v for k, v in res.items()
                                       if k != "outcome"}}}})
            return
        self._send({"jsonrpc": "2.0", "id": rid, "result": {
            "outcome": outcome,
            "_meta": {"steering": {k: v for k, v in res.items()
                                   if k != "outcome"}}}})

    def _detached_turn(self, sid_acp, text):
        """steering 在空闲会话上起的新回合：没有 JSON-RPC 请求持有它，
        输出全部经 session/update 流出（claude-agent-acp 的既有默认行为）。"""
        self._prompt_lock.acquire()
        try:
            self._run_prompt(None, sid_acp, text)
        except Exception as e:
            log(f"detached steering turn failed: {e}")
        finally:
            self._prompt_lock.release()

    def _run_prompt(self, rid, sid_acp, text):
        """单轮:思考/正文/工具卡/用量全部实时转发 ACP。"""
        emitted = {"n": 0}

        def emit(chunk):
            emitted["n"] += 1
            self._send({"jsonrpc": "2.0", "method": "session/update", "params": {
                "sessionId": sid_acp,
                "update": {"sessionUpdate": "agent_message_chunk",
                           "content": {"type": "text", "text": chunk}}}})

        def think(chunk):
            """思考逐 token → ACP agent_thought_chunk(与 claude-acp 同款)。"""
            self._send({"jsonrpc": "2.0", "method": "session/update", "params": {
                "sessionId": sid_acp,
                "update": {"sessionUpdate": "agent_thought_chunk",
                           "content": {"type": "text", "text": chunk}}}})

        tool_names = {}   # toolCallId → 工具名

        def tool_cb(tool_key, info):
            """ZCode 工具轨迹 → ACP tool_call 卡片(契约=claude-agent-acp)。
            子代理派发卡保留:app-server 传输拿不到子会话流(订阅/read 均
            "Session is not active"),Task 卡的任务描述+子代理回报就是
            主时间线里可得的全部细节;右侧面板由 subagent_spawned 另行呈现。"""
            upd = _tool_acp_update(tool_key, info, tool_names)
            self._send({"jsonrpc": "2.0", "method": "session/update",
                        "params": {"sessionId": sid_acp, "update": upd}})

        def subagent_cb(action, zid, data):
            """引擎子会话 → 客户端子代理面板(claude-acp 线协议):
            spawn/state 落在父会话,内容流(思考/正文/工具卡)落在
            zcsub-<子会话id> 自己的 ACP 会话 id 下——客户端按
            subagentSessionId 关联路由。

            数据源=子会话 resume 激活后的持久化转录(思考/正文/工具全文)
            + 父流上 source="subagent" 的 tool.updated(带完整 input/output)。"""
            child_sid = f"zcsub-{zid}"
            if action == "spawn":
                log(f"subagent spawned: {child_sid} {data.get('name', '')[:40]}")
                self._send({"jsonrpc": "2.0", "method": "session/update", "params": {
                    "sessionId": sid_acp,
                    "update": {"sessionUpdate": "subagent_spawned",
                                "subagentSessionId": child_sid,
                                "name": data.get("name") or "子代理",
                                "task": data.get("task") or "",
                                "capabilities": {}}}})
            elif action == "state":
                log(f"subagent state: {child_sid} -> {data.get('state')}")
                self._send({"jsonrpc": "2.0", "method": "session/update", "params": {
                    "sessionId": sid_acp,
                    "update": {"sessionUpdate": "subagent_state_update",
                                "subagentSessionId": child_sid,
                                "state": data.get("state") or "completed"}}})
            elif action == "thought":
                self._send({"jsonrpc": "2.0", "method": "session/update", "params": {
                    "sessionId": child_sid,
                    "update": {"sessionUpdate": "agent_thought_chunk",
                                "content": {"type": "text",
                                             "text": data.get("text") or ""}}}})
            elif action == "text":
                self._send({"jsonrpc": "2.0", "method": "session/update", "params": {
                    "sessionId": child_sid,
                    "update": {"sessionUpdate": "agent_message_chunk",
                                "content": {"type": "text",
                                             "text": data.get("text") or ""}}}})
            elif action == "tool":
                if _is_subagent_tool(data) or (data.get("name") or "").lower() in ("task", "agent"):
                    return
                upd = _tool_acp_update(data.get("tid") or f"{zid}:{data.get('name', 'tool')}",
                                        data, sub_tool_names)
                self._send({"jsonrpc": "2.0", "method": "session/update",
                            "params": {"sessionId": child_sid, "update": upd}})

        sub_tool_names = {}

        self.zcode.turn_active = True
        try:
            final = self.zcode.send(text, on_update=emit, thought_cb=think,
                                    usage_cb=lambda u: self._send_usage(sid_acp, u),
                                    tool_cb=tool_cb, subagent_cb=subagent_cb)
            if emitted["n"] == 0 and final:
                emit(final)
            stop = "cancelled" if (self.zcode._turn or {}).get("stopped") else "end_turn"
            if rid is not None:
                self._send({"jsonrpc": "2.0", "id": rid,
                            "result": {"stopReason": stop}})
        except Exception as e:
            if rid is None:
                log(f"detached turn error: {e}")
            else:
                self._send({"jsonrpc": "2.0", "id": rid, "error":
                            {"code": -32000, "message": str(e)}})
        finally:
            # 轮已结束：之后来的权限请求一律直接拒绝，不再转发弹框
            self.zcode.turn_active = False

    def _send_usage(self, sid_acp, u):
        """usage_update → 上下文圆环（used/size/cost 是 ACP 标准字段）。

        引擎的用量明细（缓存命中统计、占用分类 breakdown）在 ACP 里没有标准
        字段，按协议惯例挂在 `_meta.zcode.contextUsage`（不认识它的客户端会
        忽略）。明细只在**变化时**才带——usage 帧每几秒发一次，不去重会把
        同一份 breakdown 反复写进客户端转录。"""
        update = {"sessionUpdate": "usage_update",
                  "used": u.get("used", 0),
                  "size": u.get("size", 1000000),
                  "cost": u.get("cost")}
        meta = {}
        if isinstance(u.get("cache"), dict):
            meta["cache"] = u["cache"]
        if isinstance(u.get("breakdown"), list) and u["breakdown"]:
            meta["breakdown"] = u["breakdown"]
        if meta:
            fp = (sid_acp, json.dumps(meta, ensure_ascii=False, sort_keys=True))
            if fp != self._last_usage_meta:
                self._last_usage_meta = fp
                update["_meta"] = {"zcode": {"contextUsage": meta}}
        self._send({"jsonrpc": "2.0", "method": "session/update", "params": {
            "sessionId": sid_acp, "update": update}})

    def _catalog_opts(self):
        return (getattr(self, "catalog", {}) or {}).get("configOptions") or []

    def _engine(self, cwd):
        """确保有引擎客户端（session/list 等可能发生在 session/new 之前）。"""
        if self.zcode and self.zcode.proc.poll() is None:
            return self.zcode
        self.zcode = ZCode(cwd or os.getcwd())
        self.zcode.permission_cb = self._on_permission
        self.zcode.ui_cb = self._on_user_input
        return self.zcode

    @staticmethod
    def _engine_sid(acp_sid):
        return (acp_sid or "").removeprefix("zc-")

    def _subscribe(self, zsid):
        rid = self.zcode.next_id; self.zcode.next_id += 1
        self.zcode._send({"id": rid, "method": "session/subscribe",
                          "params": {"sessionId": zsid,
                                     "deliveryKind": "desktop-continuous",
                                     "includeSnapshot": False}})

    def _list_sessions(self, params):
        """session/list → 引擎 session/store 列表 → ACP SessionInfo。"""
        cwd = params.get("cwd")
        try:
            zc = self._engine(cwd)
            engine_params = {"includeArchived": False, "limit": 50}
            if cwd:
                engine_params["workspace"] = {"workspacePath": cwd,
                                              "workspaceKey": "acp-list"}
            r = zc.engine_call("session/list", engine_params, timeout=20) or {}
            out = []
            from datetime import datetime, timezone
            for s in r.get("sessions") or []:
                sid = str(s.get("sessionId") or s.get("id") or "")
                if not sid:
                    continue
                ws = s.get("workspace") or {}
                updated = s.get("updatedAt")
                try:
                    updated_iso = datetime.fromtimestamp(
                        updated / 1000, tz=timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ") \
                        if isinstance(updated, (int, float)) else None
                except Exception:
                    updated_iso = None
                info = {"sessionId": f"zc-{sid}",
                        "cwd": ws.get("workspacePath") or cwd or os.getcwd()}
                if s.get("title"):
                    info["title"] = s["title"]
                if updated_iso:
                    info["updatedAt"] = updated_iso
                out.append(info)
            return out
        except Exception as e:
            log(f"session/list failed: {e}")
            return []

    def _acp_commands(self):
        """当前已知的原生斜杠命令 → ACP 形状。"""
        return [{"name": c["name"], "description": c["description"],
                 **({"input": {"hint": c["inputHint"]}}
                    if c.get("inputHint") else {})}
                for c in (getattr(self.zcode, "slash_commands", None) or [])]

    def _repush_commands(self):
        """延迟补推一次命令/模式（防止首帧没被客户端接住）。"""
        try:
            if not self.acp_session:
                return
            self._push_slash_commands(self._acp_commands())
            self._send({"jsonrpc": "2.0", "method": "session/update", "params": {
                "sessionId": self.acp_session, "update": {
                    "sessionUpdate": "current_mode_update",
                    "currentModeId": self.permission_mode}}})
            log(f"repush mode={self.permission_mode}")
        except Exception as e:
            log(f"repush failed: {e}")

    def _push_slash_commands(self, commands):
        """原生斜杠命令 → ACP available_commands_update（输入 / 时的菜单数据源）。"""
        if not self.acp_session:
            return
        self._send({"jsonrpc": "2.0", "method": "session/update", "params": {
            "sessionId": self.acp_session, "update": {
                "sessionUpdate": "available_commands_update",
                # ACP 规范字段名是 availableCommands；之前写成 commands，
                # 客户端反序列化失败 → / 菜单永远空（实测确认）。
                "availableCommands": [{"name": c["name"],
                              "description": c["description"],
                              **({"input": {"hint": c["inputHint"]}}
                                 if c.get("inputHint") else {})}
                             for c in commands]}}})

    def _run_slash_command(self, text):
        """把 / 命令映射到 ZCode 原生方法。返回 ("handled", text) 或
        ("passthrough", text)（交给模型，如自定义命令）。"""
        m = re.match(r"^/([A-Za-z0-9_-]+)(?:\s+(.*))?$", text.strip(), re.S)
        if not m:
            return "passthrough", text
        name, args = m.group(1).lower(), (m.group(2) or "").strip()
        zc = self.zcode
        sid = zc.session_id
        if name == "compact":
            p = {"sessionId": sid}
            if args:
                p["instructions"] = args
            out, res = None, None
            for attempt in range(3):
                r = zc.engine_call_raw("session/compact", p, timeout=600)
                if isinstance(r, dict) and isinstance(r.get("result"), dict):
                    res = r["result"]
                    break
                err = (r or {}).get("error") or {}
                msg = err.get("message") or ""
                log(f"/compact 失败(第{attempt+1}次): "
                    f"{json.dumps(r, ensure_ascii=False)[:160]}")
                # -32010 引擎忙(如 goal 续跑中) → 等一会重试
                if err.get("code") == -32010 and attempt < 2:
                    time.sleep(8)
                    continue
                out = ("compact 暂时不可用：" + msg) if msg else "（compact 已完成，无摘要文本）"
            if res is not None:
                out = res.get("response") or "（compact 已完成，无摘要文本）"
                log(f"/compact -> state="
                    f"{(res.get('compact') or {}).get('state')}")
            return "handled", out
            out = (res or {}).get("response") or "（compact 已完成，无摘要文本）"
            state = ((res or {}).get("compact") or {}).get("state")
            log(f"/compact -> state={state} raw={json.dumps(r, ensure_ascii=False)[:200]}")
            return "handled", out
        if name == "goal":
            a = args.split(None, 1)
            head = (a[0].lower() if a else "show")
            if head in ("show", "pause", "resume", "clear"):
                action, obj = head, None
            elif head == "replace" and len(a) > 1:
                action, obj = "replace", a[1]
            elif head in ("set", "replace"):
                action, obj = "replace", (a[1] if len(a) > 1 else "")
            else:
                action, obj = "replace", args
            p = {"sessionId": sid, "action": action}
            if obj:
                p["objective"] = obj
            r = zc.engine_call("session/goal", p, timeout=120)
            res = (r or {}).get("result") if isinstance(r, dict) else None
            out = (res or {}).get("response") or "（goal 已更新）"
            return "handled", out
        if name == "mode":
            # 兜底：UI 选择器没渲染出来时，用 /mode <档位> 手动切换
            arg = (args or "").strip().lower()
            alias = {"default": "default", "confirm": "default", "ask": "default",
                     "acceptedits": "acceptEdits", "edit": "acceptEdits",
                     "autoedit": "acceptEdits", "plan": "plan", "readonly": "plan",
                     "bypasspermissions": "bypassPermissions", "yolo": "bypassPermissions",
                     "full": "bypassPermissions", "完全访问": "bypassPermissions",
                     "计划模式": "plan", "自动编辑": "acceptEdits", "变更前确认": "default"}
            target = alias.get(arg) or next((m["id"] for m in ACP_MODES
                                             if m["id"].lower() == arg or m["name"] == args), None)
            if not target:
                return "handled", ("用法：/mode <档位>\n可用档位：" +
                                   "、".join(f"{m['id']}({m['name']})" for m in ACP_MODES))
            self._set_mode_internal(target)
            cur = next(m for m in ACP_MODES if m["id"] == target)
            return "handled", f"权限模式已切换为：{cur['name']}（引擎 {MODE_TO_ENGINE[target]}）"
        if name == "plan":
            # 切到计划模式（复用 set_mode 的引擎同步与 UI 推送），任务文本照常发送
            self._set_mode_internal("plan")
            return "passthrough", (args if args else
                                   "请先给出完成当前任务前的工作计划，经我确认后再动手。")
        if name == "init":
            return "handled", (
                "请检查当前工作区并创建或更新 AGENTS.md 说明文件：总结项目结构、"
                "构建/测试命令与约定，使后续 agent 能快速上手。"
                + (f"\n补充要求：{args}" if args else ""))
        # 自定义命令/未知命令 → 交给引擎(可能是 skill 形式的自定义命令)
        return "passthrough", text

    def _set_mode_internal(self, new_mode):
        if new_mode not in MODE_TO_ENGINE:
            return
        self.permission_mode = new_mode
        if self.zcode and self.zcode.session_id:
            rr = self.zcode.engine_call(
                "session/setMode",
                {"sessionId": self.zcode.session_id,
                 "mode": MODE_TO_ENGINE[new_mode]}, timeout=15)
            log(f"mode -> {new_mode} (engine {MODE_TO_ENGINE[new_mode]}, "
                f"{'ok' if rr is not None else 'unsupported'})")
        self._send({"jsonrpc": "2.0", "method": "session/update", "params": {
            "sessionId": self.acp_session, "update": {
                "sessionUpdate": "current_mode_update",
                "currentModeId": new_mode}}})
        # config option 侧的「安全模式」同步：客户端有 configOptions 时用配置项
        # 渲染选择器（那个下拉框才是用户实际操作的控件），不推更新的话切换后
        # 它会停在旧值。
        try:
            opts = (getattr(self, "catalog", {}) or {}).get("configOptions")
            if opts:
                for opt in opts:
                    if opt.get("id") == "mode":
                        opt["currentValue"] = new_mode
                self._send({"jsonrpc": "2.0", "method": "session/update", "params": {
                    "sessionId": self.acp_session, "update": {
                        "sessionUpdate": "config_option_update",
                        "configOptions": opts}}})
        except Exception as e:
            log(f"mode option update push failed: {e}")

    def _resume_session(self, params):
        zsid = self._engine_sid(params.get("sessionId"))
        if not zsid:
            return {"error": "sessionId required"}
        try:
            cwd = params.get("cwd") or (self.zcode and self.zcode.cwd) or os.getcwd()
            zc = self._engine(cwd)
            # 会话身份 = 它自己的 workspace(path+key,list 记录里带)——
            # registry 推送和 resume 必须用同一个,否则引擎判模型不可用
            ws = {"workspacePath": cwd, "workspaceKey": "acp-list"}
            lr = zc.engine_call("session/list", {"includeArchived": False, "limit": 50,
                                                  "workspace": dict(ws)}, timeout=25)
            for s in (lr or {}).get("sessions") or []:
                if s.get("sessionId") == zsid and s.get("workspace"):
                    ws = s["workspace"]
                    break
            # 前置:把 v2 渠道推给工作区(恢复 provider 注册表)
            registry = _v2_registry()
            if registry:
                rr = zc.engine_call("workspace/updateProviderRegistry",
                                    {"workspace": dict(ws), "registry": registry},
                                    timeout=30)
                if rr and rr.get("status") not in ("applied", "unchanged"):
                    log(f"registry push status: {rr.get('status')}")
            mcp = params.get("mcpServers")
            if isinstance(mcp, list) and mcp:
                zc.mcp_servers = engine_mcp_servers(mcp)
            rp = {"sessionId": zsid, "workspace": ws}
            if zc.mcp_servers:
                rp["mcpServers"] = zc.mcp_servers
            r = zc.engine_call("session/resume", rp, timeout=30)
            if r is None:
                return {"error": f"zcode session/resume failed: {zsid}"}
            zc.session_id = zsid
            zc._turn = None
            # resume 的会话是从引擎 store 里取出来的 → session 行必然已在库里
            zc._persisted.set()
            capture_availability(r)
            self._subscribe(zsid)
            self.acp_session = f"zc-{zsid}"
            cat = zcode_model_catalog(self.permission_mode)
            self.catalog = cat
            return {"sessionId": self.acp_session,
                    "models": {"availableModels": cat["availableModels"],
                                "currentModelId": cat["currentModelId"]},
                    "configOptions": cat["configOptions"]}
        except Exception as e:
            return {"error": str(e)}

    def _fork_session(self, params):
        zsid = self._engine_sid(params.get("sessionId")) or \
            (self.zcode.session_id if self.zcode else None)
        if not zsid:
            return {"error": "sessionId required"}
        try:
            zc = self._engine(None)
            raw = zc.engine_call_raw("session/fork", {"sessionId": zsid}, timeout=30)
            r = (raw or {}).get("result")
            forked = (r or {}).get("forkedSessionId")
            if not forked:
                # 引擎的拒绝理由必须原样透出来。最常见的一条是
                # "No workspace checkpoint is available yet."（INVALID_STATE_TRANSITION）：
                # fork 是从**工作区检查点**分叉的，而检查点只在某一轮真的改了
                # 文件之后才产生——纯问答的会话 fork 不了。把它吞成 "failed: null"
                # 会让调用方以为是适配器坏了。
                err = (raw or {}).get("error") or {}
                detail = err.get("message") or ("no response" if raw is None else
                                                json.dumps(raw)[:200])
                code = ((err.get("data") or {}).get("code")) or err.get("code")
                return {"error": f"zcode session/fork failed: {detail}"
                                 + (f" [{code}]" if code else "")}
            zc.session_id = forked
            zc._turn = None
            # fork 是从工作区检查点复制出来的会话，引擎建它时就落了盘
            zc._persisted.set()
            self._subscribe(forked)
            self.acp_session = f"zc-{forked}"
            return {"sessionId": self.acp_session}
        except Exception as e:
            return {"error": str(e)}

    def _close_session(self, params):
        zsid = self._engine_sid(params.get("sessionId"))
        if not zsid or not self.zcode:
            return {}
        try:
            self.zcode.engine_call("session/close", {"sessionId": zsid}, timeout=15)
        except Exception as e:
            log(f"session/close engine error: {e}")
        if self.zcode.session_id == zsid:
            self.zcode.session_id = None
            self.zcode._turn = None
            try:
                self.zcode.proc.kill()
            except Exception:
                pass
            self.zcode = None
        return {}

    def _on_user_input(self, params):
        """ZCode 询问用户（AskUserQuestion 形状）→ ACP elicitation/create 表单。
        返回引擎应答：{action:"accept", content:{answer}} | {action:"decline"}。"""
        payload = params.get("input") if isinstance(params.get("input"), (dict, list)) \
            else params
        questions = []
        if isinstance(payload, dict):
            questions = payload.get("questions") or []
        elif isinstance(payload, list):
            questions = payload

        properties, required, per_q = {}, [], []
        for i, q in enumerate(questions if isinstance(questions, list) else []):
            if not isinstance(q, dict):
                continue
            header = str(q.get("header") or f"问题{i + 1}")
            text = str(q.get("question") or header)
            options = q.get("options") or []
            labels = [str(o.get("label") or o.get("value") or o) for o in options
                      if isinstance(o, (dict, str))]
            values = [str(o.get("value") or o.get("label") or o) for o in options
                      if isinstance(o, (dict, str))]
            key = f"q{i}"
            per_q.append({"key": key, "header": header, "labels": labels,
                          "values": values, "text": text})
            if labels:
                if q.get("multiSelect"):
                    properties[key] = {"type": "array",
                                       "description": f"{header}：{text}",
                                       "items": {"type": "string", "enum": labels}}
                else:
                    properties[key] = {"type": "string",
                                       "description": f"{header}：{text}",
                                       "enum": labels}
                required.append(key)
            else:
                properties[key] = {"type": "string", "description": f"{header}：{text}"}
                required.append(key)
        if not per_q:
            # 形状未知 → 自由文本
            properties["answer"] = {"type": "string",
                                    "description": json.dumps(params, ensure_ascii=False)[:300]}
            required = ["answer"]
            per_q = [{"key": "answer", "header": "回答", "labels": [], "values": [],
                      "text": "请输入"}]

        message = "；".join(q["text"] for q in per_q)[:500] or "请提供输入"
        perm_id = f"ui-{uuid.uuid4().hex[:8]}"
        import queue as _q
        q_queue = _q.Queue()
        self.acp_permission_waiters[perm_id] = q_queue
        self._send({"jsonrpc": "2.0", "id": perm_id,
                    "method": "elicitation/create",
                    "params": {"mode": "form",
                               "sessionId": self.acp_session,
                               "message": message,
                               "requestedSchema": {"type": "object",
                                                    "properties": properties,
                                                    "required": required}}})
        log(f"user-input {perm_id} -> elicitation/create ({len(per_q)} 问)")
        try:
            outcome = q_queue.get(timeout=600)
        except _q.Empty:
            outcome = {}
        self.acp_permission_waiters.pop(perm_id, None)
        res = outcome.get("result") if isinstance(outcome.get("result"), dict) else outcome
        action = res.get("action")
        content = res.get("content") or {}
        if action != "accept":
            log(f"user-input {perm_id} -> decline ({action})")
            return {"action": "decline"}
        # 收集答案 → 引擎要的 {action:"accept", content:{answer: 文本}}
        parts = []
        for q in per_q:
            v = content.get(q["key"])
            if v is None:
                continue
            if isinstance(v, list):
                idx = [q["labels"].index(str(x)) if str(x) in q["labels"] else None
                       for x in v]
                vals = [q["values"][j] if j is not None else str(x)
                        for x, j in zip(v, idx)]
                parts.append("; ".join(vals))
            else:
                s = str(v)
                if s in q["labels"] and q["values"]:
                    s = q["values"][q["labels"].index(s)]
                parts.append(s)
        answer = " ".join(p for p in parts if p).strip() or "确认"
        log(f"user-input {perm_id} -> accept: {answer[:120]}")
        return {"action": "accept", "content": {"answer": answer}}

    def _on_permission(self, params):
        """ZCode 权限请求 → 原样透传：
        1) ZCODE_ACP_APPROVAL_BRIDGE=1 且审批目录存在时 → 文件式外部审批桥
           （headless / 自研客户端；协议见 README）
        2) 否则 → ACP session/request_permission（客户端渲染行内审批卡片）
        返回 'allow'|'deny'。"""
        tool_name = params.get("toolName", "tool")
        brief = json.dumps(params.get("input", {}), ensure_ascii=False)[:300]

        # 渠道 1：文件式外部审批桥
        pend_dir = approval_dir()
        if approval_bridge_enabled() and os.path.isdir(pend_dir):
            rec_id = uuid.uuid4().hex[:12]
            rec = {"id": rec_id, "agent": "zcode", "tool_name": tool_name,
                   "brief": brief, "payload": params, "created": time.time(),
                   "status": "pending", "via": None, "note": None}
            with open(os.path.join(pend_dir, f"{rec_id}.json"), "w") as f:
                json.dump(rec, f, ensure_ascii=False, indent=1)
            log(f"permission #{rec_id} {tool_name} -> approval bridge")
            deadline = time.time() + 300
            decision = "deny"
            while time.time() < deadline:
                try:
                    with open(os.path.join(pend_dir, f"{rec_id}.json")) as f:
                        rec = json.load(f)
                except Exception:
                    break
                if rec.get("status") != "pending":
                    decision = rec["status"]
                    break
                time.sleep(1)
            log(f"permission #{rec_id} -> {decision}")
            return decision

        # 渠道 2：ACP 原样透传（任何 ACP 客户端的行内审批卡片）
        perm_id = f"perm-{params.get('requestId', uuid.uuid4().hex[:8])}"
        import queue as _q
        q = _q.Queue()
        self.acp_permission_waiters[perm_id] = q
        opts = [{"optionId": "allow_once", "name": "Allow once", "kind": "allow_once"},
                {"optionId": "allow_always", "name": "Always allow", "kind": "allow_always"},
                {"optionId": "reject_once", "name": "Reject", "kind": "reject_once"}]
        self._send({"jsonrpc": "2.0", "id": perm_id,
                    "method": "session/request_permission",
                    "params": {"sessionId": self.acp_session,
                               "toolCall": {"toolCallId": params.get("toolCallId", perm_id),
                                            "title": f"{tool_name}: {brief[:120]}",
                                            "kind": "edit"},
                               "options": opts}})
        log(f"permission {perm_id} {tool_name} -> ACP client (卡片审批)")
        try:
            outcome = q.get(timeout=600)
        except _q.Empty:
            outcome = {"outcome": "cancelled"}
        self.acp_permission_waiters.pop(perm_id, None)
        # 队列里可能是两种形状：① ACP 客户端的完整 JSON-RPC 响应
        # {"jsonrpc","id","result":{...}}；② _deny_all_pending 塞的裸 {"outcome":…}。
        # 统一归一化再取 outcome —— 之前直接 outcome.get("outcome")，对形状①取到
        # None，又赶上"只认显式 allow"的修正，把所有授权框都拒了。
        if isinstance(outcome, dict) and isinstance(outcome.get("result"), dict):
            outcome = outcome["result"]
        outcome_val = outcome.get("outcome") if isinstance(outcome, dict) else None
        oid = (outcome_val or {}).get("optionId", "") \
            if isinstance(outcome_val, dict) else str(outcome_val or "")
        # ⚠️ 只有明确的 allow 才放行。cancelled/超时/异常一律 deny——
        # 之前这里把 cancelled 误判成 allow：终止后引擎收到 allow，子代理
        # 继续跑、继续要权限，造成"点终止后还一直弹框"。
        decision = "allow" if oid in ("allow_once", "allow_always") else "deny"
        log(f"permission {perm_id} -> {decision} ({oid or outcome_val})")
        return decision


def main():
    argv = sys.argv[1:]
    if "--version" in argv or "-v" in argv:
        print("zcode-acp 0.7.0 (local bridge to ZCode app-server; 事件驱动流式+claude 卡片契约+子代理V4实时流+_session/steering guide 注入+落盘时序对齐)")
        return
    # 双模式：`run ...` = opencode 兼容的一次性 NDJSON 模式；
    # 无参数 = 常驻 ACP 服务器（stdio）
    if argv and argv[0] == "run":
        return run_oneshot(argv[1:])
    server = AcpServer()
    log(f"starting (zcode.cjs={ZC_CJS}, model={MODEL or 'config.json default'}, "
        f"approval_bridge={approval_bridge_enabled()})")
    sel = selectors.DefaultSelector()
    sel.register(sys.stdin, selectors.EVENT_READ)
    while True:
        if not sel.select(1.0):
            continue
        line = sys.stdin.readline()
        if not line:
            break
        line = line.strip()
        if not line:
            continue
        try:
            msg = json.loads(line)
        except json.JSONDecodeError:
            continue
        threading.Thread(target=server.handle, args=(msg,), daemon=True).start()


def _ev(obj):
    sys.stdout.write(json.dumps(obj) + "\n")
    sys.stdout.flush()


def run_oneshot(args):
    """`zcode-acp run --format json --dangerously-skip-permissions --dir <dir> [prompt…]`
    → opencode run --format json 兼容的 NDJSON 事件流。"""
    cwd = os.getcwd()
    prompt_parts = []
    i = 0
    while i < len(args):
        a = args[i]
        if a == "--dir" and i + 1 < len(args):
            cwd = args[i + 1]; i += 2; continue
        if a in ("--format", "-m", "--model", "--session") and i + 1 < len(args):
            if a in ("-m", "--model"):
                os.environ["ZCODE_ACP_MODEL"] = args[i + 1]
            i += 2; continue
        if a == "--session" :
            i += 2; continue
        if a.startswith("--"):
            i += 1; continue
        prompt_parts.extend(args[i:]); break
    prompt = " ".join(prompt_parts).strip()
    if not prompt:
        prompt = sys.stdin.read().strip()
    if not prompt:
        _ev({"type": "error", "error": {"name": "InputError",
                                        "data": {"message": "empty prompt"}}})
        return 1

    zc = ZCode(cwd)
    zc.permission_cb = lambda params: "allow"  # --dangerously-skip-permissions
    sid = zc.create_session()
    _ev({"type": "step_start", "sessionID": sid, "part": {"id": "s1", "type": "step_start"}})

    texts = []

    def collect(chunk):
        texts.append(chunk)
        _ev({"type": "text", "sessionID": sid,
             "part": {"id": f"t{len(texts)}", "type": "text", "text": chunk}})

    zc.send(prompt, on_update=collect, timeout=int(os.environ.get("ZCODE_ACP_TIMEOUT", "1800")))

    # usage
    rid = zc.next_id; zc.next_id += 1
    zc._send({"id": rid, "method": "session/usage", "params": {"sessionId": sid}})
    time.sleep(1.5)
    usage = {}
    with zc.lock:
        r = next((m for m in reversed(zc.log) if m.get("id") == rid and isinstance(m.get("result"), dict)), None)
    if r:
        u = r["result"]
        usage = {"input": u.get("inputTokens", 0), "output": u.get("outputTokens", 0),
                 "reasoning": u.get("reasoningTokens", 0), "total": u.get("totalTokens", 0)}
    _ev({"type": "step_finish", "sessionID": sid,
         "part": {"id": "f1", "type": "step_finish", "tokens": usage, "reason": "stop"}})
    try:
        zc.proc.kill()
    except Exception:
        pass
    return 0


if __name__ == "__main__":
    main()
