"""握手与会话建立的形状校验（不花模型额度）。

形状按实测客户端（Codeg，Rust 实现）的严格解析对齐：读不懂的字段整条丢弃，
形状错了在客户端表现为"什么都没有"，只有先在这里断言才看得见。
"""


def test_initialize_shape(client):
    res = client.initialize()
    assert res["protocolVersion"] == 1
    caps = res["agentCapabilities"]
    assert caps["promptCapabilities"]["embeddedContext"] is False
    for k in ("list", "resume", "fork", "close"):
        assert k in res["sessionCapabilities"], k


def test_initialize_advertises_steering(client):
    """顶层 `_meta.steering` —— 与 claude-agent-acp / codex-acp 同位置。
    广告必须与实现一致：`method` 指的方法在 test_steering.py 里真的被调用。"""
    meta = client.initialize()["_meta"]
    st = meta["steering"]
    assert st["supported"] is True
    assert st["method"] == "_session/steering"
    assert st["tier"] == "extension"
    assert st["boundary"] == "step"
    assert st["idle"] == "promptRequired"
    assert st["settlesOwnerTurn"] is False
    assert "injected" in st["outcomes"] and "promptRequired" in st["outcomes"]


def test_session_new_shape(session):
    client, sid = session
    assert sid.startswith("zc-")
    res = next(m["result"] for m in reversed(client.messages)
               if isinstance(m.get("result"), dict)
               and m["result"].get("sessionId") == sid)
    # configOptions 必须在顶层：客户端后端只读顶层，放进 models 里等于没有
    assert isinstance(res["configOptions"], list) and res["configOptions"]
    ids = {o.get("id") for o in res["configOptions"]}
    assert "model" in ids
    models = res["models"]
    assert models["currentModelId"]
    assert any(m.get("modelId") == models["currentModelId"]
               for m in models["availableModels"])
    # 思考强度按钮：引擎的 setThoughtLevel 对任意模型任意档位都接受，
    # 所以这个能力位恒为真，否则客户端对无 variants 的模型不渲染 🧠
    assert all(m.get("supportsReasoningEffort") is True
               for m in models["availableModels"])
    assert res["modes"]["currentModeId"] == "default"
    assert {m["id"] for m in res["modes"]["availableModes"]} >= {
        "default", "acceptEdits", "plan", "bypassPermissions"}


def test_unknown_method_is_rejected(session):
    client, _ = session
    r = client.call("session/not_a_real_method", {}, timeout=20)
    assert r["error"]["code"] == -32601
