# zcode-acp

[![CI](https://github.com/Liang-HZ/zcode-acp/actions/workflows/ci.yml/badge.svg)](https://github.com/Liang-HZ/zcode-acp/actions/workflows/ci.yml)

**把 ZCode 桌面客户端的私有引擎协议桥接成标准 ACP（Agent Client Protocol），让任何 ACP 客户端都能驱动 ZCode。**

[中文](#中文) · [English](#english)

---

## 中文

### 这是什么

ZCode 桌面客户端只对外提供一套私有的 app-server 协议（`zcode.cjs app-server --stdio`），没有任何 ACP 入口。`zcode-acp` 是一个单文件 Python 实现的 ACP agent（stdio / JSON-RPC），把 ZCode 引擎的会话、流式输出、工具卡、审批、补充消息（steering）、子代理全部映射成标准 ACP 方法。

```
ACP 客户端  ──stdio/JSON-RPC──▶  zcode-acp  ──stdio/JSON-RPC──▶  zcode.cjs app-server
（Zed / Codeg / 任意 ACP 客户端）              （legacy session/* + V4 v4/*）
```

三条设计边界：

- **额度走你自己的 ZCode 订阅。** 适配器只驱动本机已安装、已登录的 ZCode 引擎，用你在 ZCode 客户端里配置的渠道；不代理任何 API、不持有、不搬运任何 key。
- **原生能力不缩水。** 逐 token 思考（`agent_thought_chunk`）、逐 token 正文（`agent_message_chunk`）、工具卡三段式、行内审批、steering 的 guide 注入、子代理的逐 token 流，全部原样透传。
- **没有中间态。** 适配器只驱动本机引擎，不落库；除账号权益的现场校验（`subscription/list` / `billing/balance`，与桌面同款、只读）外不发任何请求。唯一可选的磁盘交互是审批桥（见下）。

### 能力矩阵

每一条都有仓库内的实测用例或真机实测数据支撑（用例全部打真引擎、真模型）。

| ACP 能力 | 方法 / 事件 | 实测依据 | 已知边界 |
|---|---|---|---|
| 初始化 | `initialize` | `test_handshake.py::test_initialize_shape`：`protocolVersion=1`、`sessionCapabilities{list,resume,fork,close}`、`promptCapabilities.embeddedContext=false` | `loadSession=false` —— 引擎不支持 `session/load`，历史只能经 `session/list` + `session/resume` 回来 |
| 新建会话 | `session/new` | `test_handshake.py::test_session_new_shape`：应答顶层带 `configOptions`（mode / model / reasoning_effort），`models.availableModels` 与 ZCode 桌面端同源 | 模型目录以**引擎注册表**为真值（订阅渠道按现场权益校验进入；`~/.zcode/v2/config.json` 目录仅作兜底）；一个适配器进程同一时刻只服务一个活动会话（`session/new` 新建引擎并接管，要并行就起多个进程） |
| 会话族 | `session/list` / `resume` / `fork` / `close` | `test_session_family.py::test_list_resume_fork_close`：写文件 → list 命中 → close → resume 之后历史接上暗号 → fork 出新 id | fork 需要工作区检查点；resume 必须用会话记录自带的 `(workspacePath, workspaceKey)` 二元组 |
| 流式正文 | `agent_message_chunk` | `test_steering.py::test_steering_injects_into_running_turn`：按拼接后的全文命中暗号（逐 token 流出，逐块匹配等不到） | — |
| 逐 token 思考 | `agent_thought_chunk` | 引擎 `model.streaming.reasoning_delta` 逐条转发 | 模型不产出思考时自然为空 |
| 工具卡 | `tool_call` / `tool_call_update` | `test_cancel.py::test_cancel_stops_running_turn` 等到 `tool_call` 才发取消；三段式 `pending → in_progress → completed/failed` | `content` 必须是 `{type:"content",content:{type:"text"}}`；裸 `{type:"text"}` 会被严格解析的客户端整条丢弃 |
| 审批 | `session/request_permission` | 每个动到工具或编辑的轮都会走；`tests/acp_client.py` 必须应答 `allow_once`，否则轮挂到 600s | 600s 无应答 / 客户端回 cancelled → 一律拒绝（fail closed） |
| 取消 | `session/cancel` | `test_cancel.py::test_cancel_stops_running_turn`：`sleep 90` 的工具轮在取消后 < 45s 收口，`stopReason=cancelled` | 走引擎原生 `v4/command type=stop`（桌面端停止按钮同款）；引擎的 `session/stop` 只中断当前那次模型请求，流会被 stream recovery 接上继续生成，不能用 |
| steering | `_session/steering` | 8 条用例，见「steering」一节 | 只吃纯文本块；见下 |
| 并发 prompt | `session/prompt`（轮次进行中） | `test_steering.py::test_concurrent_prompt_goes_through_guide`：两条 prompt 收口在同一轮（settle 时间差 < 5s），回执带 `_meta.steering` 投递档位 | 注入不成立时回落顺序执行 |
| 子代理 | 子会话的 `session/update` 流 | 经 V4 通道拿子会话的逐 token 思考/正文/工具入参回参 | 只能观测与取消（`session/cancelBackgroundTask`），不能向运行中的子代理注入消息 |
| 安全模式 | `modes` + `session/set_mode` | `test_handshake.py::test_session_new_shape`：`default / acceptEdits / plan / bypassPermissions` ↔ 引擎 `build / edit / plan / yolo` | — |
| 模型与推理强度 | `configOptions` + `session/set_config_option` | `supportsReasoningEffort` 恒为真；模型列表 = 引擎注册表，订阅计划（BigModel Individual / Z.AI）按现场权益校验 + `provider/updateAccountConfig` 快照进入，`account:*` 模型的 setModel 由适配器自动附加推理档位（`test_model_layer.py` 钉住解析规则） | 订阅渠道需要本机已登录的 ZCode 账号；适配器不代理 key——account 模型的运行时鉴权经 `interaction/requestProviderRuntimeHeaders` 现场下发给引擎 |
| 认证（Terminal Auth） | `initialize.authMethods` + `authenticate` + `zcode-acp --login` | ACP Registry 的 `client.py` auth check 实测通过（`terminal` 型）；`--login` 做安装检测 → 打开 ZCode 桌面端 → 等待登录 | 登录发生在 ZCode 桌面客户端里（适配器不搬运任何凭据） |
| 上下文用量 | `usage_update` + `PromptResponse.usage` | 圆环 `used/size`：占用口径（`session/read` 的 `runtime.contextUsage.used`，与 ZCode 桌面端圆环同源）+ 模型目录 `contextWindow`。逐轮明细：`session/usage` 累计差 → `usage{input,output,thought,cachedRead,cachedWrite}`（ACP 草案 RFD，camelCase），`test_turn_usage.py` 锁住 | 客户端需支持 `usage_update`；逐轮明细是草案字段，不认识它的客户端会忽略；引擎给不出成本 → `cost` 省略（不发 `amount:0`） |
| 原生斜杠命令 | `availableCommands` | 实测引擎上报 4 条：`goal` / `compact` / `init` / `plan` | 走引擎原生方法，不进模型；命令由引擎侧决定 |
| 结构化提问 | `elicitation/create` | 引擎 `interaction/requestUserInput` → ACP 表单 | 客户端需实现 `elicitation/create`；未实现或未应答 → 引擎侧收到 decline |
| 一次性 CLI | `zcode-acp run` | `run_oneshot()`：opencode 兼容的 NDJSON 事件流（`step_start` / `text` / `step_finish`） | 该模式**自动放行全部权限**（不弹审批），只适合受控的无人值守场景 |

### 用量与成本

三条通道，各就各位：

- **圆环（`usage_update` 的 `used/size`）**：占用口径。`used` 取引擎
  `session/read` 的 `runtime.contextUsage.used`（与 ZCode 桌面端圆环同源），
  `size` 取模型目录的 `contextWindow`（投影过时时不采信）。占用读数还没出现
  （首个模型请求完成前）就不发帧——不用累计数冒充占用。
- **逐轮明细（`PromptResponse.usage`）**：ACP 草案 RFD「End-Turn Token
  Usage」的字段（camelCase：`totalTokens` / `inputTokens` / `outputTokens` /
  `thoughtTokens` / `cachedReadTokens` / `cachedWriteTokens`），取引擎
  `session/usage` 的**逐轮差值**——Codeg、Zed（beta）、claude-agent-acp 生态
  都按逐轮账本结算，给累计值会被重复累加。不认识该字段的客户端会自动忽略。
- **`_meta.zcode.contextUsage`**：引擎私有增强（缓存命中统计 `cache`、占用
  分类 `breakdown`），标准里没有对应物，认识的客户端才消费。

成本：引擎目前给不出真实成本，`usage_update.cost` 一律**省略**（RFD 里本就
optional/nullable），绝不用 `{amount: 0}` 冒充“免费”。

### Getting Started

**前置条件**

- macOS（ZCode 桌面客户端是 macOS 应用）。
- ZCode.app 已安装并已登录，引擎 bundle 在
  `/Applications/ZCode.app/Contents/Resources/glm/zcode.cjs`（本机实测路径；
  引擎在别处时用 `ZCODE_CJS` 覆盖）。
- ZCode 客户端里至少有一条可用渠道（`~/.zcode/v2/config.json`）。
- Python 3.9+（实测 3.9.6 / 3.10.11 / 3.12.12 / 3.13.2 / 3.14.3：导入、启动器与不依赖引擎的用例全部通过；完整用例跑在 3.10.11 上）。

**安装**

```bash
pipx install git+https://github.com/Liang-HZ/zcode-acp
zcode-acp --version        # zcode-acp 0.9.0
```

也可以免安装直接跑（uv 用户；ACP 客户端里 command 填 `uvx`、args 填
`--from zcode-acp==0.9.0 zcode-acp` 即可）：

```bash
uvx --from zcode-acp==0.9.0 zcode-acp
```

不想装也可以直接用仓库里的启动器（它把仓库根加进 `sys.path`，不需要安装）：

```bash
git clone https://github.com/Liang-HZ/zcode-acp
./zcode-acp/bin/zcode-acp --version
```

**在 ACP 客户端里配置**

适配器就是一个 stdio 上的 ACP agent，客户端侧只要填 `command` + `args`：

```jsonc
{
  "command": "zcode-acp",   // 或 /绝对路径/zcode-acp/bin/zcode-acp
  "args": [],
  "env": { "ZCODE_ACP_MODEL": "ark/glm-5.3-flash" }   // env 可省略
}
```

以 Zed 为例（`settings.json`）：

```json
{
  "agent_servers": {
    "ZCode": { "command": "zcode-acp", "args": [], "env": {} }
  }
}
```

Codeg 等客户端按各自的 agent registry / 自定义 agent 填法填同样的
`command` / `args` / `env` 三元组即可。

> 提示：若 PATH 上已存在另一个同名命令（`which -a zcode-acp` 会列出多个），客户端命中的是
> 排在最前的那个。把 `command` 写成绝对路径可以消除歧义（`pipx environment` 会打印 pipx 的
> bin 目录）。

**环境变量**

| 变量 | 默认 | 作用 |
|---|---|---|
| `ZCODE_CJS` | `/Applications/ZCode.app/Contents/Resources/glm/zcode.cjs` | 引擎 bundle 路径 |
| `ZCODE_ACP_MODEL` | 引擎配置里的默认模型 | `<provider>/<modelId>`，例 `ark/glm-5.3-flash`，`session/new` 后应用 |
| `ZCODE_ACP_TIMEOUT` | `1800` | `run` 模式单轮的秒级上限 |
| `ZCODE_ACP_APPROVAL_BRIDGE` | 关 | `1` = 开启文件式外部审批桥（见下） |
| `ZCODE_ACP_APPROVAL_DIR` | `~/.zcode-acp/pending` | 审批桥目录，必须已存在 |
| `ZCODE_ACP_LOG_DIR` | `~/.zcode-acp/` | 诊断日志目录（文件名带 pid，多实例不互相截断） |
| `ZCODE_ACP_TRACE` | `1` | `0` 关闭协议 trace |
| `ZCODE_ACP_THOUGHT` | `medium` | 初始推理强度 |
| `ZCODE_ACP_STEER_SETTLE_MS` | `8000` | steering 等引擎裁决落定的上限 |
| `ZCODE_ACP_STEER_PERSIST_MS` | `3000` | steering 等会话落盘信号的上限 |
| `ZCODE_ACP_STEER_FK_RETRY_MS` | `2000` | 落盘信号没等到时，外键故障的有界重试预算 |

**最短成功路径**

1. 客户端里配好 agent（上一节的 `command: zcode-acp`）。
2. 新建会话，选一个工作目录。
3. 发一句「你好」。
4. 看到正文逐 token 流出（有思考的模型会先逐 token 流出思考）。

**跑测试**

```bash
python3 -m pip install pytest   # 或 pip install pytest
pytest -v
```

- 测试**真的起本机 ZCode 引擎、真的调模型**，会消耗你自己的额度；prompt 都刻意保持很短。
- 开跑前自动探活当前模型通道（额度耗尽会被识别成「模型不可用」而不是协议 bug）；
  `ZCODE_ACP_SKIP_MODEL_PROBE=1` 可跳过探活。
- 测试客户端会自动应答 `session/request_permission`（`allow_once`），不需要人守着点授权。
- 每个用例一个唯一临时 cwd（`/tmp/zcode-acp-test-<pid>-*`，落盘时序用例用
  `/tmp/zcode-acp-fk-<pid>`）和唯一日志目录，不会和客户端里开着的 ZCode 会话、
  或并发跑的别的实例互相串。

### steering

steering 是这个适配器的差异化能力：把一条补充消息**注入正在跑的那一轮**，而不是排到下一轮。适配器在 `initialize` 应答的顶层 `_meta` 里广告：

```jsonc
"steering": { "supported": true, "method": "_session/steering",
              "tier": "extension", "boundary": "step",
              "idle": "promptRequired", "settlesOwnerTurn": false,
              "outcomes": ["injected","queued","promptRequired","startedNewTurn","failed"] }
```

每一项都有实测支撑：

| 事实 | 证据 |
|---|---|
| `tier: extension` | 引擎没有把「注入当前轮」暴露成协议方法；适配器用引擎的 `v4/command sendText requestedDelivery:"guide"` 实现 ACP 的 `_session/steering` |
| `boundary: step` | 注入在**下一个模型请求边界**被 drain，不必等工具结束。实测：三段 Bash 的轮里 `turn.steerQueued`(+20.9s) → `turn.steerDrained`(+24.9s)；零工具的纯文本轮里 43.5s → 44.1s（`toolCallCount: 0`、`modelRequestCount: 2`，同一个 turn 内改口） |
| `idle: promptRequired` | 空闲时不碰引擎，直接回 `{"outcome":"promptRequired","reason":"noRunningTurn"}`，内容留给客户端用 `session/prompt` 自己发 |
| `settlesOwnerTurn: false` | 注入返回时主回合仍在跑，之后继续产出并以 `end_turn` 自然收口，全程只有一个 `stopReason`。守门用例 `test_steering_injects_into_running_turn` |

**回执怎么定**（适配器等引擎的裁决信号，不拿「已受理」当结论）：

| 引擎信号 | 回执 |
|---|---|
| 进 guide 车道且随后 `turn.steerDrained`（`steer.state=guided`） | `{"outcome":"injected","_meta":{"steering":{"delivery":"guide","steerState":"guided","pendingInputId":…}}}` |
| `steer.state == "fellBack"` | `{"outcome":"queued", … "steerState":"fellBack","reasonCode":"guide.…"}` |
| 进了 guide 车道但 `ZCODE_ACP_STEER_SETTLE_MS` 内还没 drain | `{"outcome":"injected", …"steerState":"steering"}`（如实标注：已在 guide 车道、尚未落定） |
| 轮次结束时仍未 drain | `{"outcome":"queued", …"reason":"turnEndedBeforeDrain"}` |
| 没有活动轮 + 请求声明 `idleBehavior:"promptRequired"` | `{"outcome":"promptRequired","reason":"noRunningTurn"}` |
| 没有活动轮 + 未声明 `idleBehavior` | 起一个分离的新回合 → `{"outcome":"startedNewTurn"}` |
| 引擎拒绝 / 超时（含外键重试耗尽） | `{"outcome":"failed", …}`，`reason`/`detail` 原样透传引擎原话 |

`idleBehavior` 只接受 `promptRequired`，其它值回 `-32602`（与 claude-agent-acp 一致）；
steering 的 prompt 只取 `text` 块——guide 车道带附件会被引擎当场降级。

**引擎注入协议（逆向 + 实测）**

legacy `session/send` 做不到：它的 zod schema 里没有任何 delivery 字段，轮次进行中发过去
只会被当普通排队（实测 V4 队列项是 `delivery:{requested:"queue",admitted:"queue"}`、
`steer:{state:"notRequested"}`，本轮答案完全不受影响）。唯一的通道是 V4 命令：

```jsonc
{"id": 1042, "method": "v4/command", "params": {
  "commandId": "7d90b344d3224de8acc8212ba9dc7cbe",
  "clientId":  "zcode-acp",
  "sessionId": "sess_3579db83-a0c5-4f8b-97ba-60380cda60cf",
  "type":      "sendText",
  "payload":   {"text": "…", "requestedDelivery": "guide"},   // startNow|queue|guide
  "issuedAt":  1789545167275}}
```

应答 `{"status":"accepted", …,"result":{"type":"inputAccepted","delivery":"queue",…}}`。

> ⚠️ 应答里的 `result.delivery` **不是**裁决结果：实测 guide 被完整受理并注入成功时它照样是
> `"queue"`。真裁决在事件流（`turn.steerQueued.delivery == "guide"` → 进了 guide 车道，
> `turn.steerDrained` → 真的并进了当前 turn）和 V4 父会话帧
> （`state.updated → patch.queue.items[].steer.state`，枚举
> `notRequested | submitting | steering | guided | fellBack`）上。降级只从 V4 帧读得到：
> 引擎降级时发的 `TurnSteerDeliveryChanged` 在旧通道里落进 default 分支，看不出来。
> 已知的 `fallbackReasonCode`：`guide.turnInterrupted`（注入和取消撞在同一瞬间）、
> `guide.noToolBoundary`（轮次没有后续边界可用）、`guide.attachmentsUnsupported`
> （本适配器只往 guide 车道送纯文本，所以这一条在这里不可达）。

**会话落盘时序（FOREIGN KEY 的根因）**

引擎的 `session/create` **不写库**：sqlite（`~/.zcode/cli/db/db.sqlite`）里的 `session` 行是
**首条 `session/send` 被处理时**才插的，而 `v4/command sendText` 要往 `session_input` 插一行
带 `references session(id)` 外键的记录。抢在 `session` 行之前发 guide，引擎必然回
`fault.command.executionFailed / FOREIGN KEY constraint failed`。直连引擎的探针实测
（时间以 `session/send` 发出为 0）：

| 时刻 | 事实 |
|---|---|
| `+0.000 ~ +0.300` | `v4/command sendText guide` → FOREIGN KEY 失败（0 / 50 / 100 / 200 / 300ms 五次全炸） |
| `+0.222 ~ +0.310` | **`session` 行进库**（四次实测：0.222 / 0.242 / 0.292 / 0.310s） |
| `+0.313 ~ +0.413` | **本会话第一条 `session/event`**（`session.titleUpdated`，紧跟 `turn.started`）——稳定比 `session` 行晚 100~120ms |
| `≥ +0.500` | guide 命令一律 `accepted`（500 / 800 / 1200ms 全过） |

只建会话、不发 prompt 的空闲会话等 25s，`session` 行**始终不出现**，但 `session/read`
在 `+1.8s` 就能正常应答 —— 所以 **`session/read` 可读不等于已落盘**，不能当信号。

适配器的做法（`ZCode.steer()`）：① 等本会话第一条 `session/event` 置位的确定信号，最多等
`ZCODE_ACP_STEER_PERSIST_MS`（实测只需 313~413ms）；② 万一还是撞上 FOREIGN KEY，按
100/200/400/800ms 退避做有界重试（总预算 `ZCODE_ACP_STEER_FK_RETRY_MS`，每次换新的
`commandId`）；③ 预算用尽 → 回 `{"outcome":"failed"}` 并原样透出引擎的 `reason`/`detail`。
守门用例 `test_steering_right_after_session_new_never_hits_foreign_key`：`session/new` 后立刻
`session/prompt`，并在 100 / 300 / 500ms 各发一次 steering，三次都不许出现 FK 失败、
至少一次 `injected`。

**并发 prompt 的收口语义**

轮次进行中收到第二个 `session/prompt`：**走同一条 guide 通道注入当前轮**，不再降级排队。
这条 prompt 随后成为当前轮的搭车者——等当前轮结束时以该轮的 `stopReason` 回执，并在
`_meta.steering` 里如实带上投递档位：

```jsonc
{"stopReason":"end_turn",
 "_meta":{"steering":{"outcome":"injected","delivery":"guide",
                      "steerState":"guided","pendingInputId":"queue_…"}}}
```

选这个语义的理由：ACP 里 `session/prompt` 的应答语义是「这条提示的处理结束了」；内容既然
并进了当前轮，它的处理就随当前轮一起结束，客户端看到的是一个回合、一份输出。注入不成立
时（引擎拒绝，或期间轮次已结束）回落顺序执行：等前一轮结束后作为独立回合跑。

### 文件式审批桥（可选）

给 headless / 自研客户端用。`ZCODE_ACP_APPROVAL_BRIDGE=1` 且 `ZCODE_ACP_APPROVAL_DIR`
（默认 `~/.zcode-acp/pending`）**已存在**时，适配器不再向客户端发
`session/request_permission`，而是把待审批请求写成文件、轮询外部裁决。

写入 `<dir>/<id>.json`（`<id>` 是 12 位 hex）：

```jsonc
{"id": "9f2c1a4b8d3e", "agent": "zcode", "tool_name": "Bash",
 "brief": "{\"command\": \"rm -rf build\"}",
 "payload": { /* 引擎的原始权限请求，原样 */ },
 "created": 1789545167.27, "status": "pending", "via": null, "note": null}
```

外部进程把**同一个文件**的 `status` 改成 `allow` 或 `deny` 即完成裁决（适配器每 1s 轮询
一次同一个文件，`status` 值原样返回给引擎）。超时 300s、文件读不到、或超时前 `status`
一直是 `pending` → 一律 `deny`（fail closed）。

边界：一次裁决一份文件；只影响工具审批，`elicitation/create` 的结构化提问仍走客户端。

### 已知限制

- **macOS only**：ZCode 桌面客户端是 macOS 应用，引擎 bundle 路径默认也是 macOS 路径。
- **依赖 ZCode.app 已安装并已登录**，且客户端里有一条可用渠道。
- **`session/load` 不被引擎支持**（`loadSession=false`），历史只能 `session/list` + `session/resume`。
- **`session/fork` 需要工作区检查点**：检查点只在某一轮真的改过文件之后才产生。纯问答的
  会话 fork 不了，引擎明确报 `INVALID_STATE_TRANSITION: No workspace checkpoint is available
  yet.`（实测：同一个会话，问答轮之后 fork 报这条；换成写文件的一轮之后再 fork 立刻成功，
  应答带 `targetCheckpointId` 和 "copied 1 messages and restored 1 file"）。适配器把这条理由
  原样透给客户端，不吞成 `failed: null`。
- **主会话 → 子代理的补充消息**：引擎没有对应原语。子代理可以取消
  （`session/cancelBackgroundTask`），但不能向运行中的子代理注入消息，只能观测。
- **`session/delete`**：引擎没有这个方法。
- **`session/stop` 不是真停止**：它只 abort 当前那次模型请求，被中断的流会被引擎的
  stream recovery 当成网络抖动接上继续生成；真停止只有 `v4/command type=stop`。
- **一个适配器进程同时只服务一个活动会话**：`session/new` 会起一个新的引擎进程并接管；
  要并行会话就起多个适配器进程。
- **引擎的 unix socket 路径上限**：引擎会在 `TMPDIR` 下建 `znr-<uuid>.sock`，路径超过
  103 字节时 `listen()` 直接 EINVAL、引擎立刻崩。适配器会把过长的 `TMPDIR` 换成
  `/tmp` 下的短目录（实测样本 118 字符会炸），其余情况原样继承。
- **steering 只吃文本**：guide 车道带附件会被引擎在受理时降级
  （`fallbackReasonCode: "guide.attachmentsUnsupported"`）。

### Related work

- [Agent Client Protocol](https://agentclientprotocol.com) —— 本适配器实现的协议。
- [pulpo](https://github.com/Liang-HZ/pulpo) —— 姊妹项目，桌面壳；把本仓作为 git submodule
  挂在 `packages/adapters/zcode`。
- [Codeg](https://github.com/xintaofei/codeg) —— ACP 客户端（Apache-2.0），本适配器的实测
  客户端之一；上面「严格解析的客户端」相关约束都来自与它的互操作实测。

### License / Contributing

Apache-2.0，见 [LICENSE](LICENSE)。Issue 与 PR 直接提到
[GitHub Issues](https://github.com/Liang-HZ/zcode-acp/issues)；改动请附带实测依据
（本仓的能力描述都对应真机实测或用例）。

---

## English

### What this is

The ZCode desktop client only speaks a private app-server protocol
(`zcode.cjs app-server --stdio`) and exposes no ACP entry point. `zcode-acp` is a single-file
Python ACP agent (stdio / JSON-RPC) that maps ZCode's sessions, streaming output, tool cards,
approvals, injected messages (steering) and subagents onto standard ACP methods.

```
ACP client  ──stdio/JSON-RPC──▶  zcode-acp  ──stdio/JSON-RPC──▶  zcode.cjs app-server
(Zed / Codeg / any ACP client)                (legacy session/* + V4 v4/*)
```

Three design boundaries:

- **Your quota stays yours.** The adapter only drives the ZCode engine already installed and
  signed in on this machine, using the providers you configured in the ZCode client. It never
  proxies an API and never holds or moves a key.
- **Native capabilities pass through unwrapped.** Token-by-token thinking
  (`agent_thought_chunk`), token-by-token text (`agent_message_chunk`), three-stage tool cards,
  inline approvals, steering's guide injection and token-level subagent streams.
- **No middle state.** The adapter keeps no database; apart from the read-only live entitlement checks (`subscription/list` / `billing/balance`, mirroring the desktop host) it makes no network calls. The only
  optional disk interaction is the approval bridge (below).

### Capability matrix

Every row is backed by an in-repo test or by real-engine measurements (the tests drive the real
engine and a real model).

| ACP capability | Method / event | Evidence | Known boundary |
|---|---|---|---|
| Initialize | `initialize` | `test_handshake.py::test_initialize_shape`: `protocolVersion=1`, `sessionCapabilities{list,resume,fork,close}`, `promptCapabilities.embeddedContext=false` | `loadSession=false` — the engine has no `session/load`; history comes back via `session/list` + `session/resume` |
| New session | `session/new` | `test_handshake.py::test_session_new_shape`: top-level `configOptions` (mode / model / reasoning_effort), `models.availableModels` sourced from the engine registry | The model catalog is engine-authoritative (subscription plans enter via live entitlement checks; the local `~/.zcode/v2/config.json` is only a fallback); one adapter process serves one active session at a time (`session/new` spawns a fresh engine and takes over — run several processes for parallel sessions) |
| Session family | `session/list` / `resume` / `fork` / `close` | `test_session_family.py::test_list_resume_fork_close`: write a file → list finds it → close → resume replays the codeword from history → fork returns a new id | fork needs a workspace checkpoint; resume must use the session's own `(workspacePath, workspaceKey)` pair |
| Streaming text | `agent_message_chunk` | `test_steering.py::test_steering_injects_into_running_turn` matches a codeword against the concatenated text (per-token chunks never match piecewise) | — |
| Per-token thinking | `agent_thought_chunk` | Engine `model.streaming.reasoning_delta` forwarded line by line | Empty when the model produces no reasoning |
| Tool cards | `tool_call` / `tool_call_update` | `test_cancel.py::test_cancel_stops_running_turn` waits for `tool_call` before cancelling; three stages `pending → in_progress → completed/failed` | `content` must be `{type:"content",content:{type:"text"}}`; a bare `{type:"text"}` is dropped wholesale by strict clients |
| Approvals | `session/request_permission` | Every turn that touches tools or files; `tests/acp_client.py` must answer `allow_once` or the turn hangs until 600s | No answer within 600s / client replies cancelled → deny (fail closed) |
| Cancel | `session/cancel` | `test_cancel.py::test_cancel_stops_running_turn`: a `sleep 90` tool turn settles < 45s after cancel with `stopReason=cancelled` | Uses the engine's native `v4/command type=stop` (same path as the desktop stop button). The engine's `session/stop` only aborts the current model request and stream recovery resumes it — unusable |
| steering | `_session/steering` | 8 tests, see the steering section | Text blocks only |
| Concurrent prompt | `session/prompt` while a turn runs | `test_steering.py::test_concurrent_prompt_goes_through_guide`: both prompts settle in the same turn (< 5s apart) and the reply carries `_meta.steering` | Falls back to sequential execution when injection is refused |
| Subagents | child-session `session/update` stream | Token-level child thinking/text and full tool input/output over the V4 channel | Observable and cancellable (`session/cancelBackgroundTask`), but no way to inject a message into a running subagent |
| Safety modes | `modes` + `session/set_mode` | `test_handshake.py::test_session_new_shape`: `default / acceptEdits / plan / bypassPermissions` ↔ engine `build / edit / plan / yolo` | — |
| Model & reasoning effort | `configOptions` + `session/set_config_option` | `supportsReasoningEffort` is always true; the model list is the engine registry — subscription plans (BigModel Individual / Z.AI) enter via live entitlement checks + a `provider/updateAccountConfig` snapshot, and `account:*` models get their reasoning level attached automatically (`test_model_layer.py` pins the resolution rules) | Subscription channels require a signed-in ZCode account on this machine; no API keys are proxied — runtime auth for `account:*` models is handed to the engine on demand via `interaction/requestProviderRuntimeHeaders` |
| Auth (Terminal Auth) | `initialize.authMethods` + `authenticate` + `zcode-acp --login` | Passes the ACP Registry's `client.py` auth check (type `terminal`); `--login` checks the install, opens the ZCode desktop app and waits for sign-in | Sign-in happens in the ZCode desktop app (the adapter moves no credentials) |
| Context usage | `usage_update` + `PromptResponse.usage` | Ring `used/size`: engine occupancy (`session/read` → `runtime.contextUsage.used`, the same source the ZCode desktop ring uses) + the model catalog's `contextWindow`. Per-turn detail: `session/usage` cumulative deltas → `usage{input,output,thought,cachedRead,cachedWrite}` (draft RFD, camelCase), pinned by `test_turn_usage.py` | The client must support `usage_update`; the per-turn detail is a draft field clients may ignore; no real cost from the engine → `cost` is omitted (never `amount: 0`) |
| Native slash commands | `availableCommands` | The engine reports 4: `goal` / `compact` / `init` / `plan` | They run as native engine methods, never hitting the model; the set is engine-defined |
| Structured questions | `elicitation/create` | Engine `interaction/requestUserInput` → ACP form | The client must implement `elicitation/create`; if it does not (or does not answer), the engine receives a decline |
| One-shot CLI | `zcode-acp run` | `run_oneshot()`: opencode-compatible NDJSON (`step_start` / `text` / `step_finish`) | **Auto-approves every permission** (no prompts); unattended use only |

### Usage and cost

Three channels, each in its own place:

- **Ring (`usage_update.used/size`)**: occupancy. `used` comes from the engine's
  `session/read` → `runtime.contextUsage.used` (the same source the ZCode desktop
  ring uses); `size` comes from the model catalog's `contextWindow` (a stale
  projection is not trusted). No reading yet (before the first model request
  finishes) means no frame — never a cumulative number posing as occupancy.
- **Per-turn detail (`PromptResponse.usage`)**: the draft ACP RFD
  "End-Turn Token Usage" fields (camelCase: `totalTokens` / `inputTokens` /
  `outputTokens` / `thoughtTokens` / `cachedReadTokens` / `cachedWriteTokens`),
  computed as the per-turn **delta** of the engine's `session/usage` — Codeg,
  Zed (beta) and the claude-agent-acp ecosystem all account per turn, and a
  cumulative value would be double-counted. Clients that do not know the field
  ignore it.
- **`_meta.zcode.contextUsage`**: engine-private enrichment (cache-hit stats
  `cache`, occupancy breakdown), with no standard counterpart; only clients that
  know it consume it.

Cost: the engine cannot provide a real cost today, so `usage_update.cost` is
**omitted** (optional/nullable in the RFD) — never `{amount: 0}` pretending
to be free.

### Getting Started

**Prerequisites**

- macOS (the ZCode desktop client is a macOS app).
- ZCode.app installed and signed in; the engine bundle lives at
  `/Applications/ZCode.app/Contents/Resources/glm/zcode.cjs` (measured default path — override
  with `ZCODE_CJS` if yours differs).
- At least one usable provider configured in the ZCode client (`~/.zcode/v2/config.json`).
- Python 3.9+ (verified on 3.9.6 / 3.10.11 / 3.12.12 / 3.13.2 / 3.14.3: import, launcher and the
  engine-free tests all pass; the full suite runs on 3.10.11).

**Install**

```bash
pipx install git+https://github.com/Liang-HZ/zcode-acp
zcode-acp --version        # zcode-acp 0.9.0
```

Or run it without installing (uv users; point an ACP client's command at `uvx`
with args `--from zcode-acp==0.9.0 zcode-acp`):

```bash
uvx --from zcode-acp==0.9.0 zcode-acp
```

Prefer no install? Run the launcher straight from a checkout (it puts the repo root on
`sys.path`):

```bash
git clone https://github.com/Liang-HZ/zcode-acp
./zcode-acp/bin/zcode-acp --version
```

**Configure your ACP client**

The adapter is a plain ACP agent over stdio, so the client only needs `command` + `args`:

```jsonc
{
  "command": "zcode-acp",   // or /absolute/path/zcode-acp/bin/zcode-acp
  "args": [],
  "env": { "ZCODE_ACP_MODEL": "ark/glm-5.3-flash" }   // env is optional
}
```

For Zed (`settings.json`):

```json
{
  "agent_servers": {
    "ZCode": { "command": "zcode-acp", "args": [], "env": {} }
  }
}
```

Codeg and other clients take the same `command` / `args` / `env` triple through their own agent
registry or "custom agent" entry.

> Tip: if another `zcode-acp` already exists on your PATH (`which -a zcode-acp` lists them all),
> clients will pick the first one. Put an absolute path in `command` to remove the ambiguity
> (`pipx environment` prints pipx's bin directory).

**Environment variables**

| Variable | Default | Purpose |
|---|---|---|
| `ZCODE_CJS` | `/Applications/ZCode.app/Contents/Resources/glm/zcode.cjs` | Engine bundle path |
| `ZCODE_ACP_MODEL` | engine config default | `<provider>/<modelId>`, e.g. `ark/glm-5.3-flash`; applied after `session/new` |
| `ZCODE_ACP_TIMEOUT` | `1800` | Per-turn timeout in seconds for `run` mode |
| `ZCODE_ACP_APPROVAL_BRIDGE` | off | `1` enables the file-based external approval bridge |
| `ZCODE_ACP_APPROVAL_DIR` | `~/.zcode-acp/pending` | Bridge directory; must already exist |
| `ZCODE_ACP_LOG_DIR` | `~/.zcode-acp/` | Diagnostic log directory (pid-suffixed files, instances never truncate each other) |
| `ZCODE_ACP_TRACE` | `1` | `0` disables the protocol trace |
| `ZCODE_ACP_THOUGHT` | `medium` | Initial reasoning effort |
| `ZCODE_ACP_STEER_SETTLE_MS` | `8000` | Max wait for the engine's steering verdict |
| `ZCODE_ACP_STEER_PERSIST_MS` | `3000` | Max wait for the session-persisted signal |
| `ZCODE_ACP_STEER_FK_RETRY_MS` | `2000` | Bounded retry budget for FOREIGN KEY faults |

**Shortest path to success**

1. Point your client at the agent (`command: zcode-acp` as above).
2. Start a new session and pick a working directory.
3. Send "hello".
4. Watch the reply stream in token by token (models with reasoning stream their thinking first).

**Run the tests**

```bash
python3 -m pip install pytest
pytest -v
```

- The tests **start the real ZCode engine and call a real model**, spending your own quota;
  prompts are deliberately kept short.
- They probe the current model channel first (quota exhaustion shows up as "model unavailable"
  instead of looking like a protocol bug); set `ZCODE_ACP_SKIP_MODEL_PROBE=1` to skip.
- The test client answers `session/request_permission` (`allow_once`) automatically, so nobody
  has to babysit approval dialogs.
- Every test gets a unique temporary cwd (`/tmp/zcode-acp-test-<pid>-*`, `/tmp/zcode-acp-fk-<pid>`
  for the persistence-timing test) and its own log directory, so nothing collides with the ZCode
  sessions running in your client or with other concurrent instances.

### steering

Steering is this adapter's differentiating capability: it injects a supplemental message into the
**turn that is already running** instead of queueing it for the next one. The adapter advertises
it in the top-level `_meta` of the `initialize` reply:

```jsonc
"steering": { "supported": true, "method": "_session/steering",
              "tier": "extension", "boundary": "step",
              "idle": "promptRequired", "settlesOwnerTurn": false,
              "outcomes": ["injected","queued","promptRequired","startedNewTurn","failed"] }
```

Every field is backed by measurement:

| Fact | Evidence |
|---|---|
| `tier: extension` | The engine exposes no protocol method for "inject into the running turn"; the adapter implements `_session/steering` on top of the engine's `v4/command sendText requestedDelivery:"guide"` |
| `boundary: step` | The injection drains at the **next model request boundary**, not after the tool finishes. Measured: in a three-Bash-step turn `turn.steerQueued` (+20.9s) → `turn.steerDrained` (+24.9s); in a zero-tool text turn 43.5s → 44.1s (`toolCallCount: 0`, `modelRequestCount: 2` — the answer changed within the same turn) |
| `idle: promptRequired` | When idle it never touches the engine and answers `{"outcome":"promptRequired","reason":"noRunningTurn"}`, leaving the content to the client's own `session/prompt` |
| `settlesOwnerTurn: false` | When the injection returns, the owner turn is still running; it keeps producing and closes naturally with `end_turn` — exactly one `stopReason` overall. Guarded by `test_steering_injects_into_running_turn` |

**How the receipt is decided** (the adapter waits for the engine's verdict instead of treating
acceptance as a conclusion):

| Engine signal | Receipt |
|---|---|
| Admitted to the guide lane and later `turn.steerDrained` (`steer.state=guided`) | `{"outcome":"injected","_meta":{"steering":{"delivery":"guide","steerState":"guided","pendingInputId":…}}}` |
| `steer.state == "fellBack"` | `{"outcome":"queued", … "steerState":"fellBack","reasonCode":"guide.…"}` |
| In the guide lane but not drained within `ZCODE_ACP_STEER_SETTLE_MS` | `{"outcome":"injected", …"steerState":"steering"}` (honest: admitted, not yet settled) |
| Still not drained when the turn ends | `{"outcome":"queued", …"reason":"turnEndedBeforeDrain"}` |
| No running turn + request declared `idleBehavior:"promptRequired"` | `{"outcome":"promptRequired","reason":"noRunningTurn"}` |
| No running turn + no `idleBehavior` | Starts a detached new turn → `{"outcome":"startedNewTurn"}` |
| Engine refused / timed out (including exhausted FK retries) | `{"outcome":"failed", …}` with the engine's own `reason`/`detail` passed through verbatim |

`idleBehavior` only accepts `promptRequired`; anything else returns `-32602` (matching
claude-agent-acp). Steering takes `text` blocks only — attachments on the guide lane are
downgraded by the engine on admission.

**The engine injection protocol (reverse-engineered + measured)**

Legacy `session/send` cannot do it: its zod schema has no delivery field at all, so sending
mid-turn is accepted but treated as plain queueing (measured: the V4 queue item is
`delivery:{requested:"queue",admitted:"queue"}`, `steer:{state:"notRequested"}`, and the turn's
answer is unaffected). The only channel is a V4 command:

```jsonc
{"id": 1042, "method": "v4/command", "params": {
  "commandId": "7d90b344d3224de8acc8212ba9dc7cbe",
  "clientId":  "zcode-acp",
  "sessionId": "sess_3579db83-a0c5-4f8b-97ba-60380cda60cf",
  "type":      "sendText",
  "payload":   {"text": "…", "requestedDelivery": "guide"},   // startNow|queue|guide
  "issuedAt":  1789545167275}}
```

The reply is `{"status":"accepted", …,"result":{"type":"inputAccepted","delivery":"queue",…}}`.

> ⚠️ That `result.delivery` is **not** the verdict: measured, it is still `"queue"` when a guide
> is fully accepted and injected. The real verdict lives in the event stream
> (`turn.steerQueued.delivery == "guide"` → admitted to the guide lane, `turn.steerDrained` →
> actually merged into the current turn) and in the V4 parent-session frames
> (`state.updated → patch.queue.items[].steer.state`, enum
> `notRequested | submitting | steering | guided | fellBack`). Downgrades are only visible in V4
> frames: the engine's `TurnSteerDeliveryChanged` falls into the default branch of the old
> channel's mapper. Known `fallbackReasonCode`s: `guide.turnInterrupted` (injection and cancel
> raced), `guide.noToolBoundary` (no later boundary in the turn), `guide.attachmentsUnsupported`
> (unreachable here — this adapter only sends plain text on the guide lane).

**Session persistence timing (the FOREIGN KEY root cause)**

The engine's `session/create` **does not write the database**: the `session` row in sqlite
(`~/.zcode/cli/db/db.sqlite`) is inserted only when the **first `session/send` is processed**,
while `v4/command sendText` inserts a `session_input` row with a `references session(id)` foreign
key. Sending a guide before that row exists always yields
`fault.command.executionFailed / FOREIGN KEY constraint failed`. Probe measurements against the
engine directly (t = 0 at `session/send`):

| Time | Fact |
|---|---|
| `+0.000 ~ +0.300` | `v4/command sendText guide` → FOREIGN KEY failure (all five probes at 0 / 50 / 100 / 200 / 300ms) |
| `+0.222 ~ +0.310` | **the `session` row lands** (four measurements: 0.222 / 0.242 / 0.292 / 0.310s) |
| `+0.313 ~ +0.413` | **the session's first `session/event`** (`session.titleUpdated`, immediately followed by `turn.started`) — consistently 100–120ms after the row |
| `≥ +0.500` | guide commands are always `accepted` (500 / 800 / 1200ms all pass) |

For an idle session that was only created and never prompted, the `session` row never appears
within 25s while `session/read` answers at `+1.8s` — so **a readable `session/read` does not mean
the row is persisted** and cannot be used as a signal.

What the adapter does (`ZCode.steer()`): ① wait for the definite signal (the session's first
`session/event`), at most `ZCODE_ACP_STEER_PERSIST_MS` (measured need: 313–413ms); ② if it still
hits the FOREIGN KEY fault, retry with 100/200/400/800ms backoff within
`ZCODE_ACP_STEER_FK_RETRY_MS`, fresh `commandId` each time; ③ when the budget is exhausted reply
`{"outcome":"failed"}` with the engine's `reason`/`detail` verbatim. Guarded by
`test_steering_right_after_session_new_never_hits_foreign_key`: prompt immediately after
`session/new` and steer at 100 / 300 / 500ms — no FK failure allowed, and at least one must
report `injected`.

**What a concurrent prompt means**

A second `session/prompt` while a turn is running **goes through the same guide lane into the
current turn** instead of falling back to queueing. That prompt then rides the current turn: it is
answered when the turn ends, with the turn's `stopReason`, and the receipt carries the delivery
tier honestly:

```jsonc
{"stopReason":"end_turn",
 "_meta":{"steering":{"outcome":"injected","delivery":"guide",
                      "steerState":"guided","pendingInputId":"queue_…"}}}
```

The rationale: in ACP, a `session/prompt` reply means "this prompt has been handled". Once its
content has been merged into the current turn, that handling ends with the turn — the client sees
one turn and one output. When injection fails (engine refusal, or the turn ended meanwhile) it
falls back to sequential execution as an independent turn.

### File-based approval bridge (optional)

For headless or custom clients. With `ZCODE_ACP_APPROVAL_BRIDGE=1` and an **existing**
`ZCODE_ACP_APPROVAL_DIR` (default `~/.zcode-acp/pending`), the adapter stops sending
`session/request_permission` to the client and instead writes each pending approval to a file,
polling for an external verdict.

It writes `<dir>/<id>.json` (`<id>` is 12 hex chars):

```jsonc
{"id": "9f2c1a4b8d3e", "agent": "zcode", "tool_name": "Bash",
 "brief": "{\"command\": \"rm -rf build\"}",
 "payload": { /* the engine's raw permission request, unchanged */ },
 "created": 1789545167.27, "status": "pending", "via": null, "note": null}
```

The external process edits the **same file**'s `status` to `allow` or `deny` (the adapter polls
that file once a second and returns the `status` value to the engine as the decision). A 300s
timeout, an unreadable file, or a `status` still at `pending` → `deny` (fail closed).

Boundaries: one file per decision; only tool approvals are bridged — `elicitation/create`
questions still go to the client.

### Known limitations

- **macOS only**: the ZCode desktop client is a macOS app and the default engine bundle path is a
  macOS path.
- **Requires ZCode.app installed and signed in**, with at least one usable provider.
- **`session/load` is unsupported by the engine** (`loadSession=false`); history is reachable only
  through `session/list` + `session/resume`.
- **`session/fork` needs a workspace checkpoint**: checkpoints only appear after a turn that
  actually changed files. A pure Q&A session cannot be forked and the engine says so explicitly:
  `INVALID_STATE_TRANSITION: No workspace checkpoint is available yet.` (measured: same session,
  fork after a Q&A turn returns this; after a file-writing turn it succeeds immediately with a
  `targetCheckpointId` and "copied 1 messages and restored 1 file"). The adapter passes the reason
  through instead of swallowing it as `failed: null`.
- **Parent → subagent supplemental messages**: the engine has no primitive for it. Subagents can be
  cancelled (`session/cancelBackgroundTask`) but not injected into; they are observable only.
- **`session/delete`**: the engine has no such method.
- **`session/stop` is not a real stop**: it aborts only the current model request and the engine's
  stream recovery reconnects the interrupted stream as if it were network jitter. A real stop is
  `v4/command type=stop`.
- **One active session per adapter process**: `session/new` spawns a fresh engine and takes over;
  run several processes for parallel sessions.
- **Engine unix socket path limit**: the engine creates `znr-<uuid>.sock` under `TMPDIR` and
  `listen()` fails with EINVAL once the path exceeds 103 bytes, crashing the engine silently. The
  adapter swaps an over-long `TMPDIR` for a short directory under `/tmp` (a measured 118-char
  sample does crash it) and inherits everything else untouched.
- **Steering is text-only**: attachments on the guide lane are downgraded by the engine on
  admission (`fallbackReasonCode: "guide.attachmentsUnsupported"`).

### Related work

- [Agent Client Protocol](https://agentclientprotocol.com) — the protocol this adapter implements.
- [pulpo](https://github.com/Liang-HZ/pulpo) — sister project, a desktop shell; it mounts this repo
  as a git submodule at `packages/adapters/zcode`.
- [Codeg](https://github.com/xintaofei/codeg) — an ACP client (Apache-2.0) and one of the clients
  this adapter was measured against; the "strict client" constraints above come from that
  interoperability work.

### License / Contributing

Apache-2.0, see [LICENSE](LICENSE). Issues and PRs go to
[GitHub Issues](https://github.com/Liang-HZ/zcode-acp/issues); please bring measurements —
every capability claim in this repo maps to a real-engine test or measurement.
