"""文件式外部审批桥（`ZCODE_ACP_APPROVAL_BRIDGE`）——不起引擎。

README「文件式审批桥」描述的协议就由这两条用例守住：写什么文件、外部改哪个
字段、超时/读不到时 fail closed。
"""
import json
import os
import threading
import time

import zcode_acp

TOOL = {"toolName": "Bash", "input": {"command": "rm -rf build"}}


def _resolve(dirpath, status):
    """扮演外部裁决进程：等文件出现，把 status 改掉。"""
    deadline = time.time() + 30
    while time.time() < deadline:
        files = [f for f in os.listdir(dirpath) if f.endswith(".json")]
        if files:
            p = os.path.join(dirpath, files[0])
            rec = json.loads(open(p).read())
            rec["status"] = status
            with open(p, "w") as f:
                json.dump(rec, f)
            return
        time.sleep(0.05)
    raise AssertionError("approval bridge never wrote a record")


def test_bridge_allow_roundtrip(tmp_path, monkeypatch):
    monkeypatch.setenv("ZCODE_ACP_APPROVAL_BRIDGE", "1")
    monkeypatch.setenv("ZCODE_ACP_APPROVAL_DIR", str(tmp_path))
    srv = zcode_acp.AcpServer()
    seen = {}

    def resolver():
        deadline = time.time() + 30
        while time.time() < deadline:
            files = [f for f in os.listdir(tmp_path) if f.endswith(".json")]
            if files:
                p = os.path.join(tmp_path, files[0])
                rec = json.loads(open(p).read())
                seen.update(rec)
                rec["status"] = "allow"
                with open(p, "w") as f:
                    json.dump(rec, f)
                return
            time.sleep(0.05)
        raise AssertionError("approval bridge never wrote a record")

    t = threading.Thread(target=resolver)
    t.start()
    try:
        decision = srv._on_permission(dict(TOOL))
    finally:
        t.join(30)

    assert decision == "allow"
    assert seen["agent"] == "zcode"
    assert seen["tool_name"] == "Bash"
    assert seen["status"] == "pending"
    assert seen["id"] and len(seen["id"]) == 12
    assert seen["payload"]["toolName"] == "Bash"
    assert "rm -rf build" in seen["brief"]
    for k in ("created", "via", "note"):
        assert k in seen, k


def test_bridge_deny_roundtrip(tmp_path, monkeypatch):
    monkeypatch.setenv("ZCODE_ACP_APPROVAL_BRIDGE", "1")
    monkeypatch.setenv("ZCODE_ACP_APPROVAL_DIR", str(tmp_path))
    srv = zcode_acp.AcpServer()
    t = threading.Thread(target=_resolve, args=(str(tmp_path), "deny"))
    t.start()
    try:
        decision = srv._on_permission(dict(TOOL))
    finally:
        t.join(30)
    assert decision == "deny"


def test_bridge_read_failure_denies(tmp_path, monkeypatch):
    """记录被外部删掉/读不到 → fail closed，不许默认放行。

    这条路径下没有任何东西会把 status 改成 allow：适配器要么因为读不到文件
    当场刹车，要么一直等到超时；两者都必须落回 deny。
    """
    monkeypatch.setenv("ZCODE_ACP_APPROVAL_BRIDGE", "1")
    monkeypatch.setenv("ZCODE_ACP_APPROVAL_DIR", str(tmp_path))
    srv = zcode_acp.AcpServer()

    def delete_it():
        deadline = time.time() + 30
        while time.time() < deadline:
            files = [f for f in os.listdir(tmp_path) if f.endswith(".json")]
            if files:
                os.remove(os.path.join(tmp_path, files[0]))
                return
            time.sleep(0.02)

    d = threading.Thread(target=delete_it)
    d.start()
    try:
        decision = srv._on_permission(dict(TOOL))
    finally:
        d.join(30)
    assert decision == "deny"
