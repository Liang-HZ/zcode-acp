"""`_session/steering` —— 真引擎、真 guide 投递。

descriptor 事实全部由这一组用例证明：
  tier=extension / boundary=step / idle=promptRequired / settlesOwnerTurn=false
"""
import ast
import json
import os
import threading
import time

import zcode_acp
from acp_client import unique_token

# 让引擎有多个"步"边界可以在其间插话：guide 在下一个模型请求边界被 drain，
# 所以主回合必须至少还有一步要走。
SLOW_PROMPT = ("Run these three shell commands one at a time in order, using the "
               "Bash tool, waiting for each to finish: `sleep 5 && echo STEP-A`, "
               "`sleep 5 && echo STEP-B`, `sleep 5 && echo STEP-C`. "
               "Then report which letters you saw.")


def _steer_request(token):
    return ("URGENT CORRECTION: stop the remaining steps, run no more commands. "
            "Reply with exactly the single word %s and nothing else." % token)


def _wait_first_tool(client, timeout=120):
    u = client.wait_update(
        lambda u: (u.get("update") or {}).get("sessionUpdate") == "tool_call",
        timeout=timeout)
    assert u is not None, "主回合没有进入工具阶段：" + client.tail_stderr(1500)
    return u


class _Bg:
    """后台发一个请求，记录它是什么时候被应答的。"""

    def __init__(self, client, method, params, timeout=300):
        self.client = client
        self.response = None
        self.settled_at = None
        self.rid = client.send_request(method, params)
        self._t = threading.Thread(target=self._run, args=(timeout,), daemon=True)
        self._t.start()

    def _run(self, timeout):
        try:
            self.response = self.client.wait_response(self.rid, timeout)
        except Exception as e:                       # pragma: no cover - 诊断用
            self.response = {"error": {"message": repr(e)}}
        self.settled_at = time.monotonic()

    def join(self, timeout=300):
        self._t.join(timeout)
        assert self.response is not None, "请求没有应答"
        return self.response


def test_steering_injects_into_running_turn(session):
    """轮次进行中的 `_session/steering` → 引擎 guide 投递 → 回执 injected，
    且 **不会提前 settle 主回合**（"steering 提前结算主回合"那类 bug 的守门断言）。"""
    client, sid = session
    token = unique_token()
    turn = _Bg(client, "session/prompt",
               {"sessionId": sid, "prompt": [{"type": "text", "text": SLOW_PROMPT}]})
    _wait_first_tool(client)
    time.sleep(1.5)
    assert turn.response is None, "主回合在注入之前就结束了，这一轮不算数"

    t_before = time.monotonic()
    res = client.result("_session/steering", {
        "sessionId": sid,
        "prompt": [{"type": "text", "text": _steer_request(token)}],
        "_meta": {"steering": {"idleBehavior": "promptRequired"}}}, timeout=120)
    t_steer = time.monotonic()

    assert res["outcome"] == "injected", res
    meta = res["_meta"]["steering"]
    assert meta["delivery"] == "guide"
    # guided = 引擎已把它并进当前 turn 的消息历史（turn.steerDrained）
    assert meta["steerState"] == "guided", meta
    assert meta["pendingInputId"]

    # settlesOwnerTurn = false：注入返回时主回合还活着
    assert turn.response is None, "注入把主回合提前结算了（settlesOwnerTurn 应为 false）"

    r = turn.join(300)
    assert "error" not in r, r
    assert r["result"]["stopReason"] == "end_turn"
    assert turn.settled_at > t_steer, "主回合没有在注入之后继续跑"

    # boundary=step：注入在主回合内被消化，回答体现 guide 的内容
    text = client.text_so_far(sid)
    assert token in text, f"guide 内容没有生效，本轮正文：{text[-400:]!r}"
    # 主回合只有一个应答；steering 自己不产生第二个 stopReason
    stop_reasons = [m["result"]["stopReason"] for m in client.messages
                    if isinstance(m.get("result"), dict)
                    and "stopReason" in m["result"]]
    assert len(stop_reasons) == 1, stop_reasons
    print(f"[steering] injected at +{t_steer - t_before:.1f}s, "
          f"turn settled +{turn.settled_at - t_steer:.1f}s later")


def test_steering_when_idle_requires_prompt(session):
    """空闲 + `idleBehavior:"promptRequired"` → 不碰引擎，内容留给客户端。"""
    client, sid = session
    res = client.result("_session/steering", {
        "sessionId": sid,
        "prompt": [{"type": "text", "text": "anything"}],
        "_meta": {"steering": {"idleBehavior": "promptRequired"}}}, timeout=60)
    assert res == {"outcome": "promptRequired", "reason": "noRunningTurn"}
    # 没有起任何回合
    assert not [u for u in client.updates_snapshot()
                if (u.get("update") or {}).get("sessionUpdate")
                in ("agent_message_chunk", "agent_thought_chunk")]


def test_steering_when_idle_without_optin_starts_turn(session):
    """空闲 + 未声明 idleBehavior → 两份官方实现的既有默认：起一个分离的新
    回合（输出经 session/update 流出，没有 stopReason 应答）。"""
    client, sid = session
    token = unique_token()
    res = client.result("_session/steering", {
        "sessionId": sid,
        "prompt": [{"type": "text",
                    "text": f"Reply with exactly the single word {token}."}]},
        timeout=60)
    assert res == {"outcome": "startedNewTurn"}
    assert client.wait_text(token, sid, timeout=240) is not None, \
        "分离回合没有产出：" + client.tail_stderr(1500)
    # 分离回合不占用任何 JSON-RPC 请求 → 不应出现 stopReason 应答
    assert not [m for m in client.messages
                if isinstance(m.get("result"), dict) and "stopReason" in m["result"]]


def test_steering_rejects_unknown_idle_behavior(session):
    client, sid = session
    r = client.call("_session/steering", {
        "sessionId": sid, "prompt": [{"type": "text", "text": "x"}],
        "_meta": {"steering": {"idleBehavior": "startNewTurn"}}}, timeout=30)
    assert r["error"]["code"] == -32602
    assert "idleBehavior" in r["error"]["message"]


def test_steering_rejects_empty_prompt(session):
    client, sid = session
    assert client.call("_session/steering",
                       {"sessionId": sid, "prompt": []},
                       timeout=30)["error"]["code"] == -32602
    assert client.call("_session/steering",
                       {"prompt": [{"type": "text", "text": "x"}]},
                       timeout=30)["error"]["code"] == -32602
    assert client.call("_session/steering",
                       {"sessionId": sid,
                        "prompt": [{"type": "image", "data": "", "mimeType": "image/png"}]},
                       timeout=30)["error"]["code"] == -32602


def test_concurrent_prompt_goes_through_guide(session):
    """轮次进行中的并发 `session/prompt` → 同样走 guide 注入（不再降级排队）。
    这条 prompt 搭上当前轮，回执如实标注投递档位。"""
    client, sid = session
    token = unique_token()
    first = _Bg(client, "session/prompt",
                {"sessionId": sid, "prompt": [{"type": "text", "text": SLOW_PROMPT}]})
    _wait_first_tool(client)
    time.sleep(1.5)
    assert first.response is None

    second = _Bg(client, "session/prompt",
                 {"sessionId": sid,
                  "prompt": [{"type": "text", "text": _steer_request(token)}]})
    r2 = second.join(300)
    r1 = first.join(60)

    assert "error" not in r1 and "error" not in r2, (r1, r2)
    meta = r2["result"]["_meta"]["steering"]
    assert meta["outcome"] == "injected", meta
    assert meta["delivery"] == "guide"
    assert meta["steerState"] == "guided", meta
    assert r2["result"]["stopReason"] == "end_turn"
    assert r1["result"]["stopReason"] == "end_turn"
    # 两条请求收口在同一轮：先发的那条不会晚于后发的那条很久
    assert abs(first.settled_at - second.settled_at) < 5.0
    assert token in client.text_so_far(sid)


# ---------------------------------------------------------------- 落盘时序

FK_PROMPT = "Count from 1 to 40, one number per line, nothing else."
FK_STEER_DELAYS = (0.1, 0.3, 0.5)


class _Delayed(_Bg):
    """到点再发请求（用于卡在会话刚建好那几百毫秒里的时序用例）。"""

    def __init__(self, client, method, params, delay, timeout=300):
        self.delay = delay
        self.t0 = time.monotonic()
        self._method = method
        self._params = params
        self.client = client
        self.response = None
        self.settled_at = None
        self.rid = None
        self._t = threading.Thread(target=self._delayed_run, args=(timeout,),
                                   daemon=True)
        self._t.start()

    def _delayed_run(self, timeout):
        while time.monotonic() - self.t0 < self.delay:
            time.sleep(0.005)
        self.rid = self.client.send_request(self._method, self._params)
        self._run(timeout)


def test_steering_right_after_session_new_never_hits_foreign_key():
    """`session/new` 之后立刻 `session/prompt`，并在 100/300/500ms 各发一次
    `_session/steering`。

    引擎的 `session/create` **不写库**：sqlite 的 `session` 行是首条
    `session/send` 被处理时才插的（实测 send 后 222~310ms），而
    `v4/command sendText` 要往 `session_input` 插一行带
    `references session(id)` 外键的记录 —— 抢在前面发，引擎回
    `fault.command.executionFailed / FOREIGN KEY constraint failed`。
    适配器必须等"已落盘"信号（本会话第一条 `session/event`）再发，
    所以这三次里一次都不许出现 FK 失败，且至少有一次真的注入成功。"""
    from acp_client import AcpClient

    cwd = "/tmp/zcode-acp-fk-%d" % os.getpid()
    os.makedirs(cwd, exist_ok=True)
    client = AcpClient(cwd=cwd)
    try:
        client.initialize()
        sid = client.new_session()["sessionId"]
        turn = _Bg(client, "session/prompt",
                   {"sessionId": sid,
                    "prompt": [{"type": "text", "text": FK_PROMPT}]})
        steers = [_Delayed(client, "_session/steering",
                           {"sessionId": sid,
                            "prompt": [{"type": "text",
                                        "text": "Also say DONE at the very end."}],
                            "_meta": {"steering": {"idleBehavior": "promptRequired"}}},
                           delay, timeout=180)
                  for delay in FK_STEER_DELAYS]
        outcomes = []
        for delay, s in zip(FK_STEER_DELAYS, steers):
            r = s.join(240)
            assert "error" not in r, (delay, r)
            res = r["result"]
            blob = json.dumps(res, ensure_ascii=False)
            assert "FOREIGN KEY" not in blob.upper(), (delay, res)
            assert res["outcome"] != "failed", (delay, res, client.tail_stderr(2000))
            assert res["outcome"] in ("injected", "queued", "promptRequired"), \
                (delay, res)
            outcomes.append((delay, res["outcome"]))
        turn.join(300)
        assert any(o == "injected" for _, o in outcomes), \
            (outcomes, client.tail_stderr(2000))
        print("[fk] %s" % outcomes)
    finally:
        client.close()


def test_steering_outcomes_enum_matches_implementation(client):
    """`initialize` 广告的 `_meta.steering.outcomes` = 适配器实际可能返回的
    枚举（源码里的 `STEERING_OUTCOMES`，含最终态 `failed`）。"""
    tree = ast.parse(open(zcode_acp.__file__).read())
    declared = next(ast.literal_eval(n.value) for n in tree.body
                    if isinstance(n, ast.Assign)
                    and getattr(n.targets[0], "id", "") == "STEERING_OUTCOMES")
    assert set(declared) == {"injected", "queued", "promptRequired",
                             "startedNewTurn", "failed"}
    assert client.initialize()["_meta"]["steering"]["outcomes"] == declared
