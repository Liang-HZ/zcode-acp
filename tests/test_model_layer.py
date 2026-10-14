"""模型层纯函数用例：档位解析、自愈指纹、权益判定（不出网、不打模型）。

这些是 2026-10-14 从 codeg 桥移植过来的"引擎注册表/订阅权益"层的守门断言：
档位解析错了引擎会直接拒 setModel（reasoning-level-missing），权益判定错了
订阅渠道会在目录里凭空出现/消失。
"""
import sys
import os

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from zcode_acp import (  # noqa: E402
    is_model_selection_failure, model_selection, resolve_reasoning_level,
    _level_choice,
)


def test_account_model_gets_max_reasoning_level():
    """account:* 模型引擎强制要求档位；解析不到任何档位信息时应给 "max"。"""
    sel = model_selection("account:no-such-plan", "no-such-model")
    assert sel == {"providerId": "account:no-such-plan",
                   "modelId": "no-such-model",
                   "options": {"reasoningLevel": "max"}}


def test_level_choice_prefers_default_then_max():
    assert _level_choice(["low", "high", "max"], "high") == "high"
    assert _level_choice(["low", "high", "max"], None) == "max"
    assert _level_choice(["low", "medium"], None) == "medium"
    assert _level_choice([], None) is None


def test_requested_level_used_when_supported():
    assert resolve_reasoning_level("account:x", "y", requested="high") == "high"


def test_model_selection_without_level_for_unknown_provider():
    """非 account、无任何档位信息 → 不带 options（引擎对无档位模型可接受）。"""
    sel = model_selection("some-custom-provider", "no-such-model")
    assert "options" not in sel


def test_is_model_selection_failure_fingerprints():
    engine_payload = (
        '{"error": {"type": "unknown_error", "code": "CONFIGURATION_ERROR", '
        '"message": "Select a model before continuing", '
        '"detail": "Model creation failed"}}')
    assert is_model_selection_failure(f"zcode turn failed: {engine_payload}")
    assert is_model_selection_failure(
        "Error: Select a model before choosing reasoning effort")
    assert not is_model_selection_failure("some unrelated engine failure")
    assert not is_model_selection_failure(None)
