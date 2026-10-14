# zcode-acp

[![CI](https://github.com/Liang-HZ/zcode-acp/actions/workflows/ci.yml/badge.svg)](https://github.com/Liang-HZ/zcode-acp/actions/workflows/ci.yml)
[![PyPI](https://img.shields.io/pypi/v/zcode-acp)](https://pypi.org/project/zcode-acp/)
[![Python](https://img.shields.io/badge/python-3.9%2B-blue)](https://www.python.org/downloads/)
[![License](https://img.shields.io/badge/license-Apache--2.0-blue)](LICENSE)

[English](README.md) · **简体中文**

**用任意 ACP 客户端驱动 [ZCode](https://zcode.z.ai)——Zed、Codeg，或你自己的客户端。**

ZCode（智谱的 GLM 桌面编程助手）只提供私有的 app-server 协议，没有任何 ACP 入口。`zcode-acp`
是一个单文件 Python 实现的 ACP agent，把两者在 stdio 上桥接起来，原生能力一件不少——逐
token 思考、流式正文、工具卡、行内审批、运行中轮的补充消息（steering）、子代理——同时额度
始终是你自己的：

```
ACP 客户端  ──stdio/JSON-RPC──▶  zcode-acp  ──stdio/JSON-RPC──▶  zcode.cjs app-server
（Zed / Codeg / 任意 ACP 客户端）              （legacy session/* + V4 v4/*）
```

- **不代理任何 API。** 适配器只驱动本机已安装、已登录的 ZCode 引擎，用你在 ZCode 客户端里配
  置的渠道；不持有、不搬运任何 key。
- **没有中间态。** 不落库、没有自己的会话；除只读的账号权益校验（`subscription/list` /
  `billing/balance`，与桌面端同款）外不发任何网络请求。

## 功能

- **流式输出** —— 逐 token 思考（`agent_thought_chunk`）与正文（`agent_message_chunk`）；三段
  式工具卡，入参回参全文。
- **会话族** —— 新建 / 列出 / 恢复 / fork / 关闭，支持历史回放与工作区检查点。
- **模型** —— 模型列表与引擎注册表同源：订阅计划（BigModel Individual / Z.AI）会出现且可切
  换；推理档位是一等选项。
- **审批** —— 每个动到工具或文件的操作都走 `session/request_permission`，fail closed；另有
  可选的文件式审批桥服务 headless 客户端。
- **Steering** —— 把补充消息注入**正在跑的那一轮**（`_session/steering`），投递回执如实；
  轮次进行中的第二条 prompt 走同一条通道。
- **子代理** —— 原生子代理的逐 token 流、工具卡与生命周期都会转发给客户端。
- **用量** —— 上下文圆环（`usage_update`）、逐轮 token 明细（`PromptResponse.usage`），以及
  引擎私有的缓存/占用增强。
- **原生命令** —— 引擎上报的斜杠命令：`goal` / `compact` / `init` / `plan`。
- **认证** —— Terminal Auth：`zcode-acp --login` 检查本机引擎后把登录交给 ZCode 桌面端。
- **Headless** —— `zcode-acp run` 输出 opencode 兼容的 NDJSON 事件流；会自动放行全部权限，
  仅适合受控的无人值守场景。

以上每一条都有仓库内的用例或真机实测支撑——完整 ACP 面、逐项证据与已知边界见
[docs/capability-matrix.md](docs/capability-matrix.md)。

## 环境要求

- macOS——ZCode 桌面客户端是 macOS 应用。
- [ZCode](https://zcode.z.ai) 已安装并已登录，客户端内至少有一条可用渠道。
- Python 3.9+（实测 3.9.6 / 3.10.11 / 3.12.12 / 3.13.2 / 3.14.3）。

## 安装

从 PyPI：

```bash
pipx install zcode-acp        # 或 pip install zcode-acp
zcode-acp --version
```

免安装直接跑（uv 用户）：

```bash
uvx --from zcode-acp zcode-acp --version
```

或直接用仓库里的启动器（不需要安装）：

```bash
git clone https://github.com/Liang-HZ/zcode-acp
./zcode-acp/bin/zcode-acp --version
```

## 快速开始

1. 在 ACP 客户端里指向本适配器——它就是一个 stdio 上的 ACP agent，客户端只需要 `command`
   和可选的 `args` / `env`：

   ```jsonc
   { "command": "zcode-acp", "args": [], "env": {} }
   ```

   Zed 为例（`settings.json`）：

   ```json
   {
     "agent_servers": {
       "ZCode": { "command": "zcode-acp", "args": [], "env": {} }
     }
   }
   ```

   Codeg 等客户端按各自的 agent registry / 自定义 agent 填同样的三元组即可。

2. 新建会话、选好工作目录、发一句 `hello`——回复会逐 token 流出来（模型有思考时思考先行）。

3. 引擎还没登录的话，先跑一次 `zcode-acp --login`。

> PATH 上存在多个 `zcode-acp` 时（`which -a zcode-acp` 会全部列出），客户端命中的是排在最
> 前的那个。把 `command` 写成绝对路径可以消除歧义。

## 认证

`initialize` 只广告一个 `terminal` 认证方式（`zcode-login`）；`zcode-acp --login` 检查本机引
擎、打开 ZCode 桌面端并等待登录。登录完全发生在 ZCode 应用里——凭据不经过适配器。

## 配置

全部通过环境变量配置（按客户端填在 `env` 里）：

| 变量 | 默认值 | 用途 |
|---|---|---|
| `ZCODE_CJS` | `/Applications/ZCode.app/Contents/Resources/glm/zcode.cjs` | 引擎 bundle 路径 |
| `ZCODE_ACP_MODEL` | 引擎默认 | `<provider>/<modelId>`，在 `session/new` 后应用 |
| `ZCODE_ACP_THOUGHT` | `medium` | 初始推理档位 |
| `ZCODE_ACP_TIMEOUT` | `1800` | `run` 模式的单轮超时（秒） |
| `ZCODE_ACP_LOG_DIR` | `~/.zcode-acp/` | 诊断日志目录（按 pid 分文件，实例之间互不截断） |
| `ZCODE_ACP_TRACE` | `1` | `0` 关闭协议 trace |
| `ZCODE_ACP_APPROVAL_BRIDGE` / `ZCODE_ACP_APPROVAL_DIR` | 关 / `~/.zcode-acp/pending` | 文件式审批桥（见下） |
| `ZCODE_ACP_STEER_SETTLE_MS` / `ZCODE_ACP_STEER_PERSIST_MS` / `ZCODE_ACP_STEER_FK_RETRY_MS` | `8000` / `3000` / `2000` | steering 时序参数——见 [docs/steering.md](docs/steering.md) |

## 可选：文件式审批桥

给 headless / 自研客户端用。`ZCODE_ACP_APPROVAL_BRIDGE=1` 且 `ZCODE_ACP_APPROVAL_DIR` **已存
在**时，适配器不再向客户端发 `session/request_permission`，而是把每条待审批写成一个文件：

```jsonc
{"id": "9f2c1a4b8d3e", "agent": "zcode", "tool_name": "Bash",
 "brief": "{\"command\": \"rm -rf build\"}",
 "payload": { /* 引擎的原始权限请求，原样 */ },
 "created": 1789545167.27, "status": "pending"}
```

外部进程把**同一个文件**的 `status` 改成 `allow` 或 `deny`（适配器每秒轮询，把该值原样返回
给引擎）。超时 300s、文件读不到、或超时前一直是 `pending` → 一律 `deny`（fail closed）。只
影响工具审批——`elicitation/create` 的结构化提问仍走客户端。

## 用量上报

- **上下文圆环**（`usage_update.used/size`）——`used` 取引擎的实时占用（与桌面端圆环同源），
  `size` 取模型目录的 `contextWindow`；真实读数出现之前不发帧。
- **逐轮明细**（`PromptResponse.usage`）——按轮取差值（`totalTokens` / `inputTokens` /
  `outputTokens` / `thoughtTokens` / `cachedReadTokens` / `cachedWriteTokens`），字段来自
  ACP 的 end-turn token-usage 草案。
- **引擎增强**（`usage_update._meta.zcode.contextUsage`）——缓存命中统计与占用分类，认识的客
  户端才消费。
- **成本**——引擎给不出真实成本时一律省略，绝不用 `amount: 0` 冒充。

## 工作原理

每条能力都走引擎自己的通道：legacy app-server 方法承载会话、prompt 与工具卡；V4 通道
（`v4/*`）承载 legacy 做不到的部分——真停止、steering 投递、子代理实时流、父会话帧。逐方法
的映射与证据在 [docs/capability-matrix.md](docs/capability-matrix.md)。

## 已知限制

- **仅 macOS**；需要 ZCode.app 已安装并已登录。
- **一个适配器进程同时只服务一个活动会话**——`session/new` 会起新引擎并接管；并行会话请起多
  个进程。
- **`session/load` 不被引擎支持**——历史只能经 `session/list` + `session/resume` 回来。
- **`session/fork` 需要工作区检查点**——只有在真的改过文件的一轮之后才能 fork。
- **子代理只能观测与取消，不能注入消息**；引擎没有 `session/delete`。
- **steering 只吃文本**——guide 车道带附件会被引擎受理时降级。

## 开发

```bash
git clone https://github.com/Liang-HZ/zcode-acp && cd zcode-acp
python3 -m pip install pytest

# 纯函数子集——CI 跑的口径，不需要引擎
ZCODE_ACP_SKIP_ENGINE=1 python3 -m pytest tests/test_model_layer.py -q

# 全量——打真引擎、真模型（消耗你的额度）
python3 -m pytest
```

全量套件会先探活当前模型通道（`ZCODE_ACP_SKIP_MODEL_PROBE=1` 可跳过），自动应答审批，并
为每个用例隔离工作目录与日志。设计笔记在 [docs/](docs/)。

## 相关项目

- [Agent Client Protocol](https://agentclientprotocol.com)——本适配器实现的协议。
- [Codeg](https://github.com/xintaofei/codeg)——互操作实测使用的 ACP 客户端。
- [pulpo](https://github.com/Liang-HZ/pulpo)——桌面壳，把本仓作为 submodule 挂载。

## License

Apache-2.0，见 [LICENSE](LICENSE)。Issue 与 PR 提到
[GitHub Issues](https://github.com/Liang-HZ/zcode-acp/issues)；请附带实测依据——本仓的能力
描述都对应真机实测或用例。
