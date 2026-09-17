"""驱动 `bin/zcode-acp` 的最小 ACP 客户端（测试用）。

真引擎、真模型、真权限应答。三条踩过坑的硬规矩写死在这里：

1. **stdin 写锁**——权限应答来自读者线程、请求来自测试线程，两者共写同一根
   管道。没锁时 JSON 行会交错，表现为"权限请求发出去了但永远没人应答"，
   轮次挂到 600s 超时。
2. **必须应答 `session/request_permission`**，形状
   `{"outcome":{"outcome":"selected","optionId":"allow_once"}}`。
3. **唯一的临时 cwd**（`/tmp/zcode-acp-test-<pid>-<n>`）+ 唯一的日志目录，
   避免和用户在客户端里开着的 ZCode 会话、以及并发跑的别的实例互相串。
"""
import json
import os
import shutil
import subprocess
import sys
import tempfile
import threading
import time
import uuid

HERE = os.path.dirname(os.path.abspath(__file__))
AGENT = os.path.join(os.path.dirname(HERE), "bin", "zcode-acp")


class AcpClient:
    def __init__(self, cwd=None, env=None):
        self.cwd = cwd or tempfile.mkdtemp(
            prefix="zcode-acp-test-%d-" % os.getpid(), dir="/tmp")
        self.log_dir = os.path.join(self.cwd, ".logs")
        os.makedirs(self.log_dir, exist_ok=True)
        e = dict(os.environ)
        e["ZCODE_ACP_LOG_DIR"] = self.log_dir
        e.setdefault("ZCODE_ACP_TRACE", "0")
        if env:
            e.update(env)
        self.stderr_path = os.path.join(self.log_dir, "agent-stderr.log")
        self.proc = subprocess.Popen(
            [sys.executable, AGENT], cwd=self.cwd,
            stdin=subprocess.PIPE, stdout=subprocess.PIPE,
            stderr=open(self.stderr_path, "w"), env=e)
        self._stdin_lock = threading.Lock()
        self._lock = threading.Lock()
        self.messages = []          # 所有收到的报文
        self.updates = []           # session/update 的 params
        self.permission_requests = []
        self._next_id = 1
        self._sent = {}
        self._reader = threading.Thread(target=self._read_loop, daemon=True)
        self._reader.start()

    # ---------------------------------------------------------------- 传输

    def _write(self, obj):
        with self._stdin_lock:
            self.proc.stdin.write((json.dumps(obj) + "\n").encode())
            self.proc.stdin.flush()

    def _read_loop(self):
        for line in self.proc.stdout:
            try:
                msg = json.loads(line)
            except Exception:
                continue
            with self._lock:
                self.messages.append(msg)
                if msg.get("method") == "session/update":
                    self.updates.append(msg.get("params") or {})
            method, rid = msg.get("method"), msg.get("id")
            if method and rid is not None:
                self._answer_agent_request(method, rid, msg.get("params") or {})

    def _answer_agent_request(self, method, rid, params):
        if method == "session/request_permission":
            with self._lock:
                self.permission_requests.append(params)
            self._write({"jsonrpc": "2.0", "id": rid, "result": {
                "outcome": {"outcome": "selected", "optionId": "allow_once"}}})
        elif method == "elicitation/create":
            self._write({"jsonrpc": "2.0", "id": rid,
                         "result": {"action": "decline"}})
        else:
            self._write({"jsonrpc": "2.0", "id": rid, "result": {}})

    # ---------------------------------------------------------------- RPC

    def send_request(self, method, params):
        with self._lock:
            rid = self._next_id
            self._next_id += 1
            self._sent[rid] = method
        self._write({"jsonrpc": "2.0", "id": rid, "method": method,
                     "params": params})
        return rid

    def wait_response(self, rid, timeout=120):
        deadline = time.time() + timeout
        while time.time() < deadline:
            with self._lock:
                r = next((m for m in reversed(self.messages)
                          if m.get("id") == rid and "method" not in m), None)
            if r is not None:
                return r
            if self.proc.poll() is not None:
                raise RuntimeError("agent exited: " + self.tail_stderr())
            time.sleep(0.05)
        raise TimeoutError(f"no response for request {rid} ({self._sent.get(rid)})")

    def call(self, method, params, timeout=120):
        return self.wait_response(self.send_request(method, params), timeout)

    def result(self, method, params, timeout=120):
        r = self.call(method, params, timeout)
        assert "error" not in r, f"{method} failed: {r['error']}"
        return r["result"]

    # ---------------------------------------------------------------- 便利

    def initialize(self):
        return self.result("initialize", {
            "protocolVersion": 1,
            "clientCapabilities": {"fs": {"readTextFile": False,
                                          "writeTextFile": False}}}, timeout=60)

    def new_session(self, timeout=180):
        return self.result("session/new", {"cwd": self.cwd, "mcpServers": []},
                           timeout=timeout)

    def updates_snapshot(self):
        with self._lock:
            return list(self.updates)

    def wait_update(self, predicate, timeout=120):
        deadline = time.time() + timeout
        while time.time() < deadline:
            for u in self.updates_snapshot():
                if predicate(u):
                    return u
            time.sleep(0.1)
        return None

    def text_so_far(self, session_id=None):
        out = []
        for u in self.updates_snapshot():
            if session_id and u.get("sessionId") != session_id:
                continue
            upd = u.get("update") or {}
            if upd.get("sessionUpdate") == "agent_message_chunk":
                out.append((upd.get("content") or {}).get("text") or "")
        return "".join(out)

    def wait_text(self, needle, session_id=None, timeout=180):
        """等正文里出现某段文字。

        ⚠️ 必须按**拼接后的全文**判定：正文是逐 token 流出的，一个 8 位的暗号
        会被切成 `'724'` / `'EF6'` / `'A7'` 好几块，逐块匹配永远等不到。"""
        deadline = time.time() + timeout
        while time.time() < deadline:
            text = self.text_so_far(session_id)
            if needle in text:
                return text
            time.sleep(0.2)
        return None

    def tail_stderr(self, n=4000):
        try:
            with open(self.stderr_path) as f:
                return f.read()[-n:]
        except Exception:
            return ""

    def engine_log(self, n=4000):
        try:
            for name in sorted(os.listdir(self.log_dir)):
                if name.startswith("engine-"):
                    with open(os.path.join(self.log_dir, name)) as f:
                        return f.read()[-n:]
        except Exception:
            pass
        return ""

    def close(self):
        try:
            self.proc.terminate()
            self.proc.wait(timeout=10)
        except Exception:
            try:
                self.proc.kill()
            except Exception:
                pass
        shutil.rmtree(self.cwd, ignore_errors=True)


def unique_token():
    return uuid.uuid4().hex[:8].upper()
