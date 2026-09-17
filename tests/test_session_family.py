"""会话族回归：list / resume / fork / close。

resume 的配方（跨进程 resume 会报 `ZCODE_RUNTIME_MODEL_UNAVAILABLE`）在适配器
里：工作区身份是 `(workspacePath, workspaceKey)` 二元组，registry 推送和 resume
必须用会话记录自带的那一对。这里用"暗号"闭环证明历史真的接上了。
"""
import os

from acp_client import unique_token


def _prompt(client, sid, text, timeout=300):
    r = client.result("session/prompt",
                      {"sessionId": sid, "prompt": [{"type": "text", "text": text}]},
                      timeout=timeout)
    assert r["stopReason"] == "end_turn", r
    return client.text_so_far(sid)


def test_fork_without_checkpoint_reports_engine_reason(session):
    """新会话还没有工作区检查点 → 引擎明确拒绝。适配器必须把理由原样透出来，
    不能吞成 `failed: null`。"""
    client, sid = session
    err = client.call("session/fork", {"sessionId": sid}, timeout=120)["error"]
    assert "checkpoint" in err["message"], err
    assert "INVALID_STATE_TRANSITION" in err["message"], err


def test_list_resume_fork_close(session):
    client, sid = session
    token = unique_token()
    # 这一轮必须真的改文件：fork 是从工作区检查点分叉的，而检查点只在某一轮
    # 动过文件之后才产生（见 test_fork_without_checkpoint_reports_engine_reason）。
    _prompt(client, sid,
            "Use the Write tool to create a file notes.txt whose only line is "
            f"{token}. Then reply with just: done")
    assert os.path.exists(os.path.join(client.cwd, "notes.txt"))

    listed = client.result("session/list", {"cwd": client.cwd}, timeout=120)
    ids = {s["sessionId"] for s in listed["sessions"]}
    assert sid in ids, f"{sid} not in {sorted(ids)[:5]}…"
    entry = next(s for s in listed["sessions"] if s["sessionId"] == sid)
    assert entry["cwd"] == client.cwd

    client.result("session/close", {"sessionId": sid}, timeout=120)

    resumed = client.result("session/resume",
                            {"sessionId": sid, "cwd": client.cwd}, timeout=180)
    assert resumed["sessionId"] == sid
    assert resumed["configOptions"]

    before = len(client.text_so_far(sid))
    _prompt(client, sid,
            "Without reading any file, what single line did you write into "
            "notes.txt? Reply with just that line.")
    assert token in client.text_so_far(sid)[before:], "resume 后历史没接上"

    forked = client.result("session/fork", {"sessionId": sid}, timeout=180)
    assert forked["sessionId"].startswith("zc-")
    assert forked["sessionId"] != sid
    client.result("session/close", {"sessionId": forked["sessionId"]}, timeout=120)
