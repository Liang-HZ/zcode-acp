"""PromptResponse.usage（逐轮差值）—— 不起引擎。

协议位：ACP 草案 RFD「End-Turn Token Usage」——逐轮 token 明细挂在
`session/prompt` 的 result.usage（camelCase）。引擎的 session/usage 是会话
累计口径，这里锁住：
  - 累计差 → ACP 字段名的映射（reasoning→thoughtTokens、
    cacheRead→cachedReadTokens、cacheCreation→cachedWriteTokens）；
  - 无增量/缺快照 → 不带 usage；负差不外泄（max(0, ·)）；
  - `_run_prompt` 把差值挂进 prompt 应答（含 cancelled 轮）；本地命令轮
    （计数器没动）不带 usage。
"""
import zcode_acp

BEFORE = {"totalTokens": 1000, "inputTokens": 600, "outputTokens": 300,
          "reasoningTokens": 100, "cacheCreationTokens": 0, "cacheReadTokens": 400}
AFTER = {"totalTokens": 1500, "inputTokens": 900, "outputTokens": 420,
         "reasoningTokens": 180, "cacheCreationTokens": 40, "cacheReadTokens": 650}


# ---------------------------------------------------------------- 纯映射

def test_delta_maps_engine_counters_to_acp_fields():
    u = zcode_acp.turn_usage_delta(BEFORE, AFTER)
    assert u == {"totalTokens": 500, "inputTokens": 300, "outputTokens": 120,
                 "thoughtTokens": 80, "cachedReadTokens": 250,
                 "cachedWriteTokens": 40}


def test_no_advance_means_no_usage():
    assert zcode_acp.turn_usage_delta(BEFORE, BEFORE) is None


def test_missing_snapshot_means_no_usage():
    assert zcode_acp.turn_usage_delta(None, AFTER) is None
    assert zcode_acp.turn_usage_delta(BEFORE, None) is None


def test_negative_delta_is_clamped():
    out = zcode_acp.turn_usage_delta(AFTER, BEFORE)
    assert out is None or all(v >= 0 for v in out.values())


def test_usage_advanced_detects_change():
    assert zcode_acp.usage_advanced(BEFORE, AFTER)
    assert not zcode_acp.usage_advanced(dict(BEFORE), BEFORE)


# ---------------------------------------------------------------- 轮次接线

class _FakeEngine:
    """只实现 _run_prompt 需要的协议：send 立即收轮，query_usage 依次给快照。"""

    turn_active = False
    _turn = {}

    def __init__(self, snapshots):
        self.snapshots = list(snapshots)

    def query_usage(self):
        return self.snapshots.pop(0) if self.snapshots else None

    def send(self, text, **kw):
        return ""


def _run(engine, rid=7):
    srv = zcode_acp.AcpServer()
    srv.zcode = engine
    sent = []
    srv._send = sent.append
    srv._run_prompt(rid, "zc-s1", "hi")
    return sent


def test_run_prompt_attaches_turn_usage():
    sent = _run(_FakeEngine([BEFORE, AFTER]))
    result = sent[-1]["result"]
    assert result["stopReason"] == "end_turn"
    assert result["usage"]["totalTokens"] == 500
    assert result["usage"]["cachedReadTokens"] == 250


def test_run_prompt_omits_usage_without_increment():
    sent = _run(_FakeEngine([BEFORE, BEFORE]))
    assert "usage" not in sent[-1]["result"]


def test_run_prompt_keeps_usage_on_cancelled_turn():
    engine = _FakeEngine([BEFORE, AFTER])
    engine._turn = {"stopped": True}
    sent = _run(engine)
    assert sent[-1]["result"]["stopReason"] == "cancelled"
    assert sent[-1]["result"]["usage"]["totalTokens"] == 500
