#!/usr/bin/env python3
"""通道探活：直连引擎跑一句最短的话，确认这条模型通道此刻真的能用。

为什么必须先探活：不少渠道是滚动配额（实测样本为 5 小时窗口），额度耗尽时轮会
以 `turn.failed`（provider 侧 `AccountQuotaExceeded`）收场。不先探活，额度
问题会被误读成适配器的协议 bug。

    python3 model_probe.py "<providerId>/<modelId>" [...]
"""
import json
import os
import subprocess
import sys
import tempfile
import threading
import time
import uuid

ZC_CJS = os.environ.get(
    "ZCODE_CJS", "/Applications/ZCode.app/Contents/Resources/glm/zcode.cjs")

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)


def _engine_env():
    """桥同款引擎环境（`zcode_acp.engine_env()`）。

    ZCode 3.12.3+ 的内置 CLI 需要宿主注入 provider 表，缺失时引擎以
    「无法定位 CLI ZCode Built-in Provider Config」立即退出——探针用裸
    `os.environ` 拉起会 BrokenPipe。探针必须与桥（以及真实客户端里跑出来的
    进程）用同一套环境，否则本地全量测试会死在探活这一步。"""
    import zcode_acp
    return zcode_acp.engine_env()


def _node():
    for cand in (os.environ.get("ZCODE_NODE"), "/usr/local/bin/node",
                 "/opt/homebrew/bin/node"):
        if cand and os.path.exists(cand):
            return cand
    from shutil import which
    return which("node") or "node"


def check(model, budget=60):
    cwd = tempfile.mkdtemp(prefix="zcode-acp-probe-%d-" % os.getpid(), dir="/tmp")
    log, lock, answered = [], threading.Lock(), set()
    nid = [900]
    slock = threading.Lock()
    proc = subprocess.Popen([_node(), ZC_CJS, "app-server", "--stdio"], cwd=cwd,
                            env=_engine_env(),
                            stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                            stderr=subprocess.DEVNULL)

    def send(o):
        with slock:
            proc.stdin.write((json.dumps(o) + "\n").encode())
            proc.stdin.flush()

    def newid():
        nid[0] += 1
        return nid[0]

    def reader():
        for line in proc.stdout:
            try:
                m = json.loads(line)
            except Exception:
                continue
            with lock:
                log.append(m)
            mid, me = m.get("id"), m.get("method")
            if me and mid is not None and "server" in str(mid):
                with lock:
                    if mid in answered:
                        continue
                    answered.add(mid)
                r = {"id": mid, "result": {}}
                if me == "session/requestRuntimePreferences":
                    r["result"] = {"nativeSearchEnhancementsEnabled": False}
                elif me == "interaction/requestOfficialMcpAuthHeaders":
                    r["result"] = {"headersApplied": False}
                elif me == "interaction/requestPermission":
                    r["result"] = {"decision": "allow"}
                send(r)

    threading.Thread(target=reader, daemon=True).start()

    def rpc(method, params, timeout=60):
        rid = newid()
        send({"id": rid, "method": method, "params": params})
        dl = time.time() + timeout
        while time.time() < dl:
            with lock:
                r = next((x for x in reversed(log) if x.get("id") == rid), None)
            if r is not None:
                return r
            time.sleep(0.2)
        return {"error": "timeout"}

    try:
        rpc("initialize", {"protocolVersion": 1, "clientCapabilities": {}}, 30)
        rpc("session/create", {"workspace": {
            "workspacePath": cwd,
            "workspaceKey": "probe-" + uuid.uuid4().hex[:8]}}, 60)
        sid = None
        with lock:
            for m in log:
                if m.get("method") == "session/requestRuntimePreferences":
                    sid = (m.get("params") or {}).get("sessionId")
        if not sid:
            return "FAIL no session"
        pid, _, mid = model.partition("/")
        rpc("session/setModel",
            {"sessionId": sid, "model": {"providerId": pid, "modelId": mid}}, 30)
        rpc("session/subscribe", {"sessionId": sid,
                                  "deliveryKind": "desktop-continuous",
                                  "includeSnapshot": False})
        send({"id": newid(), "method": "session/send",
              "params": {"sessionId": sid, "content": "hi"}})
        t0 = time.time()
        while time.time() - t0 < budget:
            time.sleep(1.0)
            with lock:
                ev = [m["params"] for m in log if m.get("method") == "session/event"]
            fail = [e for e in ev if e.get("type") == "turn.failed"]
            if fail:
                err = (fail[0].get("payload") or {}).get("error") or {}
                return "FAIL " + str(err.get("detail") or err.get("message") or err)[:160]
            if any(e.get("type") == "turn.completed" for e in ev):
                return "OK"
        return "FAIL timeout"
    finally:
        proc.kill()
        import shutil
        shutil.rmtree(cwd, ignore_errors=True)


if __name__ == "__main__":
    for m in sys.argv[1:]:
        print(f"{m:70s} -> {check(m)}", flush=True)
