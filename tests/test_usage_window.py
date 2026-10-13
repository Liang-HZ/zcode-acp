"""上下文圆环 size 的取值优先级 —— 不起引擎。

引擎 3.15 的会话投影 contextWindow 只在会话创建/水合时写一次、之后换模型
不更新（实测：create 的 `settings.model.available` 里 glm-5.3-flash=1000000，
而 `session/read` 的 `projection.contextWindow` 与拼装出的 patch 恒为 200000）。
客户端圆环按 `usage_update.size` 显示，所以适配器必须以 availability 的
目录值为准——这三条用例锁住：目录命中 > 旧来源兜底 > 1M 默认。
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
        {"id": 1, "result": {"projection": {"contextWindow": 200000}}},
    ])
    assert zc._fetch_usage()["size"] == 1000000


def test_falls_back_to_old_sources_when_catalog_misses(monkeypatch):
    monkeypatch.setattr(zcode_acp, "MODEL_AVAIL_LIST", [])
    zc = _FakeEngine(log=[
        {"id": 1, "result": {"projection": {"contextWindow": 200000}}},
    ])
    assert zc._fetch_usage()["size"] == 200000


def test_defaults_to_one_million_without_any_source(monkeypatch):
    monkeypatch.setattr(zcode_acp, "MODEL_AVAIL_LIST", [])
    zc = _FakeEngine(log=[{"id": 1, "result": {"projection": {}}}])
    assert zc._fetch_usage()["size"] == 1000000
