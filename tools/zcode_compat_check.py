#!/usr/bin/env python3
"""ZCode compatibility check — re-run after every ZCode release.

The adapter couples to ZCode at four layers. This script verifies each layer
against the locally installed ZCode build and prints PASS / WARN / FAIL:

  1. app layout   — engine bundle path, builtin provider table
  2. engine protocol — method table + error fingerprints inside zcode.cjs
  3. live engine smoke — spawn the engine, session/create, dump availability
  4. desktop-host mirrors — the files/endpoints our entitlement logic mirrors
     (provider_config.json semantics, billing/balance endpoint, telemetry mid)

Usage:
    python3 tools/zcode_compat_check.py [--skip-network]

Exit code 0 when no FAIL; 1 otherwise (WARNs do not fail).
"""
import hashlib
import json
import os
import sys
import tempfile
import time

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, ROOT)

import zcode_acp as z  # noqa: E402

PASS, WARN, FAIL = "PASS", "WARN", "FAIL"
results = []


def check(layer, name, ok, detail="", warn=False):
    status = PASS if ok else (WARN if warn else FAIL)
    results.append((layer, name, status, detail))
    print(f"  [{status}] {name}" + (f" — {detail}" if detail else ""))


def sha256_of(path):
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


# ---------------------------------------------------------------- 1. app layout
print("== 1. app layout")
check("layout", "engine bundle exists (ZC_CJS)", os.path.isfile(z.ZC_CJS), z.ZC_CJS)
if os.path.isfile(z.ZC_CJS):
    print(f"         engine sha256: {sha256_of(z.ZC_CJS)[:16]}…  "
          f"size={os.path.getsize(z.ZC_CJS)}")
builtin = z.resolve_builtin_table_path()
check("layout", "builtin provider table", bool(builtin), str(builtin))
personal = z.resolve_personal_provider_config()
check("layout", "personal provider config", bool(personal), str(personal),
      warn=True)

# ------------------------------------------------------- 2. engine protocol
print("== 2. engine protocol surface")
if os.path.isfile(z.ZC_CJS):
    src = open(z.ZC_CJS, encoding="utf-8", errors="replace").read()
    methods = [
        "session/create", "session/resume", "session/list", "session/subscribe",
        "session/send", "session/read", "session/usage", "session/setModel",
        "session/setThoughtLevel", "session/setMode", "session/fork",
        "session/close", "session/stop", "session/requestRuntimePreferences",
        "provider/updateAccountConfig", "interaction/requestPermission",
        "interaction/requestUserInput", "v4/command",
    ]
    missing = [m for m in methods if f'"{m}"' not in src]
    check("protocol", f"{len(methods)} methods the adapter calls",
          not missing, f"missing: {missing}" if missing else "")
    fingerprints = [
        "Select a model before continuing",   # model-selection failure match
        "Model creation failed",
        "turn.completed", "turn.failed",
        "model.streaming", "tool.updated",
        "reasoning_delta", "text_delta",
        "turn.steerQueued", "turn.steerDrained",
        "session.titleUpdated",
    ]
    miss_fp = [f for f in fingerprints if f not in src]
    check("protocol", f"{len(fingerprints)} event/error fingerprints",
          not miss_fp, f"missing: {miss_fp}" if miss_fp else "")
    # The adapter relies on this method NOT existing (it is a no-op on 3.15).
    # If upstream adds it back, resume-time registry push starts working again.
    check("protocol", "workspace/updateProviderRegistry still absent "
          "(adapter treats it as a no-op)", "updateProviderRegistry" not in src,
          warn=True)
    check("protocol", "session model selection shapes (settings.model)",
          '"settings"' in src and "getSessionModelSelection" in src)

# ---------------------------------------------------- 3. live engine smoke
print("== 3. live engine smoke (no model request)")
try:
    work = tempfile.mkdtemp(prefix="zcode-compat-")
    zc = z.ZCode(work)
    try:
        key = "compat-" + os.urandom(4).hex()
        zc.workspace = {"workspacePath": work, "workspaceKey": key}
        rid = zc.next_id
        zc.next_id += 1
        zc._send({"id": rid, "method": "session/create",
                  "params": {"workspace": dict(zc.workspace)}})
        res = None
        deadline = time.time() + 60
        while time.time() < deadline:
            with zc.lock:
                r = next((x for x in zc.log if x.get("id") == rid), None)
            if r:
                res = r.get("result")
                break
            time.sleep(0.3)
        check("smoke", "session/create responds", bool(res))
        if res:
            model = ((res.get("settings") or {}).get("model") or {})
            avail = model.get("available") or []
            check("smoke", "settings.model.available present", bool(avail),
                  f"{len(avail)} model(s)")
            print(f"         current: {json.dumps(model.get('current'), ensure_ascii=False)}")
            for e in avail[:12]:
                ref = e.get("ref") or {}
                print(f"           - {ref.get('providerId')}/{ref.get('modelId')}"
                      f"  disabled={e.get('disabledReason')}")
    finally:
        try:
            zc.proc.kill()
        except Exception:
            pass
except Exception as e:
    check("smoke", "engine smoke run", False, repr(e))

# ------------------------------------------------ 4. desktop-host mirrors
# This build mirrors the desktop host in three places: the credentials file
# (login wizard), provider_config.json `enabled` semantics (engine injection +
# catalog filter), and the engine's provider table (builtin file). The codeg
# bridge additionally mirrors the subscription/billing layer; that is not part
# of this distribution (see agentrelay/zcode-acp).
print("== 4. desktop-host mirrors")
try:
    creds_ok = os.path.isfile(z.CRED_PATH)
    check("host", "credentials.json (login wizard)", creds_ok, z.CRED_PATH,
          warn=True)
    pcfg = json.load(open(os.path.expanduser("~/.zcode/v2/provider_config.json")))
    rules = (((pcfg.get("config") or {}).get("providerConfigRules") or {})
             .get("providerRules") or [])
    has_enabled = all(isinstance(r.get("enabled"), bool) for r in rules) and rules
    check("host", "provider_config.json rules carry boolean enabled", has_enabled,
          f"{len(rules)} rule(s)")
    check("host", "login readiness check answers",
          isinstance(z.zcode_login_ready(), bool))
    if hasattr(z, "live_entitled_plans"):
        live = z.live_entitled_plans()
        print(f"         live entitled: {sorted(live) if live is not None else None}")
        check("host", "live entitlement check answered", live is not None,
              warn=True)
    else:
        print("         (live entitlement layer not present in this build)")
except Exception as e:
    check("host", "desktop-host files", False, repr(e))

# ---------------------------------------------------------------- summary
failed = [r for r in results if r[2] == FAIL]
warned = [r for r in results if r[2] == WARN]
print()
print(f"== summary: {len(results) - len(failed) - len(warned)} pass, "
      f"{len(warned)} warn, {len(failed)} fail")
for layer, name, status, detail in failed:
    print(f"  FAIL {layer}: {name} — {detail}")
sys.exit(1 if failed else 0)
