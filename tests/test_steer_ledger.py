"""投递台账（`ZCode._v4_scan_queue`）对引擎真实帧的判读。

这里喂进去的不是编出来的形状，而是从 `/Applications/ZCode.app/.../zcode.cjs`
真跑出来的 `v4/conversation/frame` 原文（见 README「引擎注入协议」里的实测
片段）。引擎把 guide 降级成排队时只发 `TurnSteerDeliveryChanged`，而该事件在
协议映射函数 `C6i` 里落进 default 分支（→ `"session.updated"`），旧通道分辨
不出降级——所以 fellBack 只能从 V4 帧读，这条路径必须有断言守住。

`guide.turnInterrupted` 在真机上要"注入与取消抢在同一个瞬间"才会出现
（正常情况下引擎会先把 guide drain 掉）；下面这份 payload 就是那次抢到的
现场原文。
"""
import threading

import pytest

import zcode_acp as shim

CMD = "90829f4fcb8a4db2ad13dd2c5647fca4"


def _queue_frame(item):
    """引擎真实帧的外层形状：deltas → state.updated → patch.queue.items[]。"""
    return {"kind": "deltas",
            "deltas": [{"op": "state.updated",
                        "patch": {"queue": {"items": [item], "autoDrain": False}}}]}


FELL_BACK_ITEM = {
    "queueItemId": "queue_" + CMD,
    "kind": "sendText",
    "text": "URGENT CORRECTION: ...",
    "sourceCommandId": CMD,
    "clientId": "zcode-acp",
    "attachments": [],
    "delivery": {"requested": "guide", "admitted": "queue",
                 "fallbackReasonCode": "guide.turnInterrupted"},
    "order": {"admissionSeq": 1, "queuePosition": 0},
    "steer": {"state": "fellBack", "reasonCode": "guide.turnInterrupted"},
    "dispatch": {"state": "queued"},
    "admittedAt": 1789545521134,
}

STEERING_ITEM = {
    "queueItemId": "queue_" + CMD,
    "kind": "sendText",
    "text": "URGENT CORRECTION: ...",
    "sourceCommandId": CMD,
    "clientId": "zcode-acp",
    "attachments": [],
    "delivery": {"requested": "guide", "admitted": "guide"},
    "order": {"admissionSeq": 1, "queuePosition": 0},
    "steer": {"state": "steering"},
    "dispatch": {"state": "queued"},
    "admittedAt": 1789545451814,
}


@pytest.fixture
def zc():
    """不起引擎，只要台账那几个字段。"""
    z = shim.ZCode.__new__(shim.ZCode)
    z._steer_lock = threading.Lock()
    z._steer = {CMD: {"admitted_ev": threading.Event(),
                      "settled_ev": threading.Event(),
                      "state": None, "admitted": None, "reasonCode": None,
                      "pendingInputId": None, "turn_over": False}}
    return z


def test_fellback_frame_is_read_as_queued(zc):
    zc._v4_scan_queue(_queue_frame(FELL_BACK_ITEM))
    rec = zc._steer[CMD]
    assert rec["state"] == "fellBack"
    assert rec["admitted"] == "queue"
    assert rec["reasonCode"] == "guide.turnInterrupted"
    assert rec["admitted_ev"].is_set() and rec["settled_ev"].is_set()


def test_steering_frame_admits_but_does_not_settle(zc):
    zc._v4_scan_queue(_queue_frame(STEERING_ITEM))
    rec = zc._steer[CMD]
    assert rec["state"] == "steering"
    assert rec["admitted"] == "guide"
    assert rec["admitted_ev"].is_set()
    assert not rec["settled_ev"].is_set(), "还没 drain 就不能算落定"


def test_snapshot_frame_is_read_too(zc):
    zc._v4_scan_queue({"kind": "snapshot",
                       "snapshot": {"queue": {"items": [FELL_BACK_ITEM]}}})
    assert zc._steer[CMD]["state"] == "fellBack"


def test_unrelated_command_is_ignored(zc):
    other = dict(FELL_BACK_ITEM, sourceCommandId="deadbeef")
    zc._v4_scan_queue(_queue_frame(other))
    rec = zc._steer[CMD]
    assert rec["state"] is None and not rec["admitted_ev"].is_set()


def test_turn_end_releases_waiters(zc):
    zc._steer_release()
    rec = zc._steer[CMD]
    assert rec["turn_over"] is True
    assert rec["admitted_ev"].is_set() and rec["settled_ev"].is_set()


def test_steering_text_only_takes_text_blocks():
    """guide 车道上不能带 attachments：引擎对
    `requestedDelivery=guide` + 非空 attachments 在受理时就降级
    （`fallbackReasonCode: "guide.attachmentsUnsupported"`）。"""
    f = shim.AcpServer._steering_text
    assert f([{"type": "text", "text": "a"}, {"type": "text", "text": "b"}]) == "ab"
    assert f([{"type": "image", "data": "x", "mimeType": "image/png"}]) == ""
    assert f(None) == ""
