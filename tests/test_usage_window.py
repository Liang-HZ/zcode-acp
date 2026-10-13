"""usage_update 圆环口径 —— 不起引擎。

占用（used）只认引擎的占用口径：session/read 的 runtime.contextUsage.used
（与 ZCode 桌面端圆环同源），projection.contextUsed 兜底；session/usage 的
totalTokens 是任务累计口径，不许再当占用。size 依旧三级取值：目录 > 旧来源
兜底 > 1M 默认。用例锁住：
  - 占用取 runtime.contextUsage.used / projection.contextUsed；
  - 圆环日志里混入累计值（模拟旧来源）也不得覆盖占用；
  - 目录命中 > 旧来源兜底 > 1M 默认；
  - 没有任何占用读数时 _fetch_usage 返回 None（不发帧）。
"""
import threading

import zcode_acp


class _FakeEngine(zcode_acp.ZCode):
    """跳过 __init__（不起引擎），只保留 _fetch_usage 需要的状态。"""

    def __init__(self, log=None):
        self.next_id = 900
        self.lock = threading.Lock()
        self.session_id = "sess_test"
        self.log = log or []

    def _send(self, obj):
        self.log.append({"id": obj["id"], "result": {"totalTokens": 12345}})


def _state_updated(cw):
    return {"params": {"type": "state.updated", "patch": {"model": {
        "current": {"providerId": "p", "modelId": "GLM-5.3-Flash",
                    "options": {"reasoningLevel": "max"}},
        "available": [{"ref": {"providerId": "p", "modelId": "GLM-5.3-Flash"},
                       "contextWindow": cw}]}}}}


def _read_result(used=12345, cw=200000):
    return {"id": 7, "result": {
        "runtime": {"contextUsage": {"used": used, "size": cw}},
        "projection": {"contextUsed": used, "contextWindow": cw}}}


def test_capture_availability_extracts_settings_model(monkeypatch):
    monkeypatch.setattr(zcode_acp, "MODEL_AVAIL_LIST", [])
    zcode_acp.capture_availability({"settings": {"model": {"available": [
        {"ref": {"providerId": "p", "modelId": "GLM-5.3-Flash"},
         "contextWindow": 1000000}]}}})
    assert zcode_acp.catalog_context_window("p", "GLM-5.3-Flash") == 1000000
    assert zcode_acp.catalog_context_window("p", "nope") is None


def test_catalog_window_wins_over_stale_projection(monkeypatch):
    monkeypatch.setattr(zcode_acp, "MODEL_AVAIL_LIST", [
        {"ref": {"providerId": "p", "modelId": "GLM-5.3-Flash"},
         "contextWindow": 1000000}])
    zc = _FakeEngine(log=[
        _state_updated(200000),
        _read_result(used=12345, cw=200000),
    ])
    assert zc._fetch_usage()["size"] == 1000000


def test_falls_back_to_old_sources_when_catalog_misses(monkeypatch):
    monkeypatch.setattr(zcode_acp, "MODEL_AVAIL_LIST", [])
    zc = _FakeEngine(log=[_read_result(used=12345, cw=200000)])
    u = zc._fetch_usage()
    assert u["size"] == 200000
    assert u["used"] == 12345          # 占用口径，不是 session/usage 累计


def test_defaults_to_one_million_without_any_source(monkeypatch):
    monkeypatch.setattr(zcode_acp, "MODEL_AVAIL_LIST", [])
    zc = _FakeEngine(log=[{"id": 1, "result": {"projection": {"contextUsed": 42}}}])
    u = zc._fetch_usage()
    assert u["size"] == 1000000
    assert u["used"] == 42             # projection.contextUsed 兜底


def test_used_prefers_occupancy_over_session_totals(monkeypatch):
    """日志里混入累计值（旧来源 session/usage.totalTokens）也不得覆盖占用。"""
    monkeypatch.setattr(zcode_acp, "MODEL_AVAIL_LIST", [])
    zc = _FakeEngine(log=[
        _read_result(used=209845, cw=1000000),
        {"id": 9, "result": {"totalTokens": 987654}},      # 更新但只是累计值
    ])
    u = zc._fetch_usage()
    assert u["used"] == 209845


def test_no_reading_means_no_frame(monkeypatch):
    monkeypatch.setattr(zcode_acp, "MODEL_AVAIL_LIST", [])
    zc = _FakeEngine(log=[
        {"id": 1, "result": {"runtime": {}}},
        {"id": 2, "result": {"projection": {"contextWindow": 200000}}},
    ])
    assert zc._fetch_usage() is None


def test_zero_used_is_not_a_reading(monkeypatch):
    monkeypatch.setattr(zcode_acp, "MODEL_AVAIL_LIST", [])
    zc = _FakeEngine(log=[_read_result(used=0)])
    assert zc._fetch_usage() is None
