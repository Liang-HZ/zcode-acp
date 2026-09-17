"""取消回归。

`session/cancel` 走引擎原生的 `v4/command type=stop`（桌面端停止按钮同一条
路径，停整个前台执行）。协议里的 `session/stop` 只 abort 当前那次模型请求，
被中断的流还会被引擎的 stream recovery 接上继续生成——所以不能用它。
"""
import threading
import time


def test_cancel_stops_running_turn(session):
    client, sid = session
    box = {}

    def run():
        box["r"] = client.call(
            "session/prompt",
            {"sessionId": sid, "prompt": [{"type": "text", "text":
             "Use the Bash tool to run `sleep 90 && echo DONE`, then report the output."}]},
            timeout=300)

    t = threading.Thread(target=run, daemon=True)
    t.start()
    got = client.wait_update(
        lambda u: (u.get("update") or {}).get("sessionUpdate") == "tool_call",
        timeout=120)
    assert got is not None, "没有进入工具阶段：" + client.tail_stderr(1500)
    time.sleep(1.0)
    assert "r" not in box

    t0 = time.monotonic()
    client.send_request("session/cancel", {"sessionId": sid})
    t.join(90)
    elapsed = time.monotonic() - t0
    assert "r" in box, f"取消后 {elapsed:.0f}s 主回合仍未收口"
    assert box["r"]["result"]["stopReason"] == "cancelled", box["r"]
    # `sleep 90` 还没跑完 → 收口时间必须远小于工具自身的时长
    assert elapsed < 45, f"收口耗时 {elapsed:.0f}s，没有真的停下来"
    print(f"[cancel] turn settled {elapsed:.1f}s after cancel")


def test_steering_after_cancel_reports_idle(session):
    """取消之后会话回到空闲 → steering 必须如实回 promptRequired，
    不能还声称注入进了一个已经死掉的 turn。"""
    client, sid = session
    box = {}

    def run():
        box["r"] = client.call(
            "session/prompt",
            {"sessionId": sid, "prompt": [{"type": "text", "text":
             "Use the Bash tool to run `sleep 90 && echo DONE`, then report the output."}]},
            timeout=300)

    t = threading.Thread(target=run, daemon=True)
    t.start()
    assert client.wait_update(
        lambda u: (u.get("update") or {}).get("sessionUpdate") == "tool_call",
        timeout=120) is not None
    client.send_request("session/cancel", {"sessionId": sid})
    t.join(90)
    assert "r" in box and box["r"]["result"]["stopReason"] == "cancelled"

    res = client.result("_session/steering", {
        "sessionId": sid, "prompt": [{"type": "text", "text": "hello"}],
        "_meta": {"steering": {"idleBehavior": "promptRequired"}}}, timeout=60)
    assert res == {"outcome": "promptRequired", "reason": "noRunningTurn"}
