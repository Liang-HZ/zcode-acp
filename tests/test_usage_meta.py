"""usage_update 的引擎明细扩展（`_meta.zcode.contextUsage`）—— 不起引擎。

ZCode 引擎的缓存命中统计（cache）与占用分类（breakdown）在 ACP 里没有标准
字段，适配器按协议惯例挂在 `_meta` 上；客户端不认识会忽略。用例锁住：
  - `_fetch_usage` 从引擎消息里取到明细（breakdown 只能来自 ModelComplete
    事件——`session/read` 的 `runtime.contextUsage` 里它会被引擎的合并守卫
    吞掉；cache 两个来源都行）；
  - `_send_usage` 只在明细**变化时**附带（几秒一次的 usage 帧不去重会把同一
    份 breakdown 反复写进客户端转录）；
  - 没有明细时维持原样（不带 `_meta`）；cost 只在引擎真给成本时才带，
    不发虚构的 `{amount: 0}`。
"""
import threading

import zcode_acp

CACHE = {"hitRate": 0.948, "totalCacheReadTokens": 1200, "totalInputTokens": 1265}
BREAKDOWN = [{"source": "mcp_tool_schemas", "chars": 366668},
             {"source": "messages", "chars": 50}]


class _FakeEngine(zcode_acp.ZCode):
    """跳过 __init__（不起引擎），只保留 _fetch_usage 需要的状态。"""

    def __init__(self, log=None):
        self.next_id = 900
        self.lock = threading.Lock()
        self.session_id = "sess_test"
        self.log = log or []

    def _send(self, obj):
        self.log.append({"id": obj["id"], "result": {"totalTokens": 80972}})


def _model_event(cache=None, breakdown=None):
    payload = {"content": "ok", "contextWindow": 1000000}
    if cache is not None:
        payload["cacheHit"] = cache
    if breakdown is not None:
        payload["contextUsageBreakdown"] = breakdown
    return {"method": "session/event",
            "params": {"deliveryKind": "desktop-continuous",
                       "type": "session.updated", "payload": payload}}


def _read_result(cache=None, breakdown=None):
    cu = {"used": 80972, "size": 200000}
    if cache is not None:
        cu["cache"] = cache
    if breakdown is not None:
        cu["breakdown"] = breakdown
    return {"id": 7, "result": {"runtime": {"contextUsage": cu}}}


def test_fetch_usage_takes_detail_from_model_event(monkeypatch):
    monkeypatch.setattr(zcode_acp, "MODEL_AVAIL_LIST", [
        {"ref": {"providerId": "p", "modelId": "GLM-5.3-Flash"},
         "contextWindow": 1000000}])
    zc = _FakeEngine(log=[
        {"params": {"patch": {"model": {"current": {"providerId": "p",
                                                    "modelId": "GLM-5.3-Flash"}}}}},
        _read_result(),
        _model_event(cache=CACHE, breakdown=BREAKDOWN),
    ])
    u = zc._fetch_usage()
    assert u["size"] == 1000000
    assert u["used"] == 80972                     # 占用来自 read 读数
    assert u["cache"]["hitRate"] == 0.948
    # breakdown 必须是事件里的那份（事件带 chars 形态）
    assert u["breakdown"] == BREAKDOWN
    assert "cost" not in u                        # 不发虚构成本


def test_fetch_usage_cache_falls_back_to_read_result(monkeypatch):
    monkeypatch.setattr(zcode_acp, "MODEL_AVAIL_LIST", [])
    zc = _FakeEngine(log=[_read_result(cache=CACHE)])
    u = zc._fetch_usage()
    assert u["cache"]["hitRate"] == 0.948
    assert "breakdown" not in u                   # read 源本来就没有 breakdown


def test_send_usage_attaches_meta_on_change_only():
    srv = zcode_acp.AcpServer()
    sent = []
    srv._send = sent.append
    u = {"used": 80972, "size": 1000000,
         "cache": dict(CACHE), "breakdown": BREAKDOWN}
    srv._send_usage("zc-s1", u)
    srv._send_usage("zc-s1", u)                   # 相同 -> 去重
    u2 = dict(u, cache={"hitRate": 0.5})
    srv._send_usage("zc-s1", u2)                  # 变化 -> 再带
    upd = [f["params"]["update"] for f in sent]
    assert upd[0]["_meta"]["zcode"]["contextUsage"]["breakdown"][0]["chars"] == 366668
    assert "_meta" not in upd[1]
    assert upd[2]["_meta"]["zcode"]["contextUsage"]["cache"]["hitRate"] == 0.5
    # 标准字段不受影响；没有真实成本就不带 cost（RFD 里可选/可空）
    assert upd[0]["used"] == 80972 and upd[0]["size"] == 1000000
    assert "cost" not in upd[0]
    # 引擎真给出成本时原样透传
    srv._send_usage("zc-s1", dict(u, cost={"amount": 0.12, "currency": "CNY"}))
    assert sent[-1]["params"]["update"]["cost"] == {"amount": 0.12, "currency": "CNY"}


def test_send_usage_without_detail_has_no_meta():
    srv = zcode_acp.AcpServer()
    sent = []
    srv._send = sent.append
    srv._send_usage("zc-s2", {"used": 1, "size": 1000, "cost": None})
    assert "_meta" not in sent[0]["params"]["update"]
    assert "cost" not in sent[0]["params"]["update"]
