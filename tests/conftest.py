import json
import os
import sys

import pytest

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, HERE)
sys.path.insert(0, ROOT)
from acp_client import AcpClient  # noqa: E402
from model_probe import check as probe_model  # noqa: E402

ZC_CJS = os.environ.get(
    "ZCODE_CJS", "/Applications/ZCode.app/Contents/Resources/glm/zcode.cjs")
CLI_CONFIG = os.path.expanduser("~/.zcode/cli/config.json")


def configured_model():
    """当前引擎默认模型（`~/.zcode/cli/config.json` 的 `model`）。
    `ZCODE_ACP_MODEL` 覆盖它——脚本里不写死任何通道。"""
    env = os.environ.get("ZCODE_ACP_MODEL")
    if env:
        return env
    with open(CLI_CONFIG) as f:
        return json.load(f).get("model") or ""


def pytest_report_header(config):
    return "zcode model: %s" % configured_model()


def pytest_configure(config):
    """开跑前确认引擎和模型都在：模型渠道多为滚动配额（实测样本为 5 小时窗口），
    额度耗尽时轮会以 `turn failed` 收场，不先探活就会把额度问题误读成协议 bug。

    `ZCODE_ACP_SKIP_ENGINE=1`：无本机 ZCode 的环境（如公开 CI）只跑纯函数
    用例（tests/test_model_layer.py），跳过引擎存在性与模型探活断言。"""
    if os.environ.get("ZCODE_ACP_SKIP_ENGINE") == "1":
        return
    assert os.path.exists(ZC_CJS), f"ZCode engine not found: {ZC_CJS}"
    model = configured_model()
    assert model, "no model configured (~/.zcode/cli/config.json 的 model 为空)"
    if os.environ.get("ZCODE_ACP_SKIP_MODEL_PROBE") == "1":
        return
    verdict = probe_model(model)
    assert verdict == "OK", f"model probe failed for {model}: {verdict}"


@pytest.fixture
def client():
    c = AcpClient()
    try:
        yield c
    finally:
        c.close()


@pytest.fixture
def session(client):
    """已握手 + 已建会话的客户端。返回 (client, acp_session_id)。"""
    client.initialize()
    res = client.new_session()
    return client, res["sessionId"]
