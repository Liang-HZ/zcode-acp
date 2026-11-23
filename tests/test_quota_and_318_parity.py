"""ZCode 3.18.2 (zcode.cjs 0.17.1) 协议对标与 150% 额度四层链路全量测试。

覆盖两个维度：
1. 纯函数与本地 Mock HTTP 单测（ZCODE_ACP_SKIP_ENGINE=1 可在无引擎 CI 下运行）：
   - engine_env 注入 ZCODE_APP_VERSION / ZCODE_CLIENT_LANGUAGE；
   - enc:v2:os / enc:v2:env / enc:v1 加解密闭环与 requestCredentialCipher (HJ schema)；
   - requestOfficialMcpAuthHeaders 新版 tct schema（受信任域名、未登录、个人/团队头）；
   - 10 条 builtin zhipu-account 规则映射（含 team-coding-plan / off-peak / highspeed 隐藏过滤）；
   - stale bigmodel accessToken == zcodejwttoken 拦截 + user_info id/user_id 兼容；
   - ProjectAccessToken 签发、缓存、rejectedProjectTokenFingerprint 失效重签、
     expectedAccountScope 护栏、404 位置缓存自愈、fail-closed 禁止裸 zcodejwttoken 回退。
2. 真实 zcode.cjs 引擎端到端四层闭环实测（零外部流量、零消耗真实额度）：
   - 本地自签名 TLS 上游同时扮演 Z.AI 业务面（含 /api/paas/c1f3a7e2/v2/client V4 握手）
     与 ZCode 网关面（/api/v1/agent/configs + /api/v1/ultra-zai/anthropic/v1/messages）；
   - 启动真实 zcode-acp / ZCode 引擎完成完整一轮对话，并在服务端数学验签：
     Layer 1 ProjectAccessToken 换取 + Layer 2 Ed25519 签名与 8-bit PoW +
     Layer 3 requestCredentialCipher 解密与 Ultra 路由改写 + X-ZCode-Authorization +
     Layer 4 ZCode/3.18.2 来源头。
"""
import base64
import datetime
import hashlib
import hmac
import http.server
import ipaddress
import json
import os
import socketserver
import ssl
import threading
import time
import uuid

import pytest
from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ed25519, rsa
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from cryptography.hazmat.primitives.kdf.hkdf import HKDF
from cryptography.x509.oid import NameOID

import zcode_acp as bridge


def _b64u_json(obj):
    raw = json.dumps(obj, separators=(",", ":")).encode("utf-8")
    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode("ascii")


def _make_fake_pat(api_key_id="kid_demo_001", exp_offset=3600, jti=None):
    hdr = {"alg": "HS256", "typ": "JWT", "sign_type": "SIGN"}
    pl = {
        "iss": "platform-biz",
        "aud": "wd-openapi",
        "jti": jti or uuid.uuid4().hex,
        "iat": int(time.time()),
        "exp": int(time.time()) + exp_offset,
        "api_key": api_key_id,
        "client_type": "zcode",
        "client_version": "3.18.2",
        "token_use": "project_access",
    }
    sig = base64.urlsafe_b64encode(os.urandom(32)).rstrip(b"=").decode("ascii")
    return f"{_b64u_json(hdr)}.{_b64u_json(pl)}.{sig}"


def _make_fake_zcode_jwt(sub="u_123"):
    hdr = {"alg": "RS256", "typ": "JWT"}
    pl = {"sub": sub, "iat": int(time.time()), "exp": int(time.time()) + 7200}
    sig = base64.urlsafe_b64encode(os.urandom(32)).rstrip(b"=").decode("ascii")
    return f"{_b64u_json(hdr)}.{_b64u_json(pl)}.{sig}"


@pytest.fixture(autouse=True)
def _reset_bridge_caches():
    bridge.clear_project_token_caches()
    bridge._LIVE_TEAM_CONTEXT.clear()
    with bridge._OS_KEYRING_LOCK:
        bridge._OS_KEYRING_CACHE.clear()
    yield
    bridge.clear_project_token_caches()
    bridge._LIVE_TEAM_CONTEXT.clear()
    with bridge._OS_KEYRING_LOCK:
        bridge._OS_KEYRING_CACHE.clear()


# ---------------------------------------------------------------- 1. Env & Cipher

def test_engine_env_injects_app_version_and_language(tmp_path, monkeypatch):
    v2_dir = tmp_path / ".zcode" / "v2"
    v2_dir.mkdir(parents=True)
    (v2_dir / "setting.json").write_text(json.dumps({"uiLocale": "en-US"}), encoding="utf-8")
    monkeypatch.setenv("ZCODE_DATA_BASE_DIR", str(tmp_path))
    monkeypatch.delenv("ZCODE_APP_VERSION", raising=False)
    monkeypatch.delenv("ZCODE_CLIENT_LANGUAGE", raising=False)
    monkeypatch.setattr(bridge, "zcode_app_version", lambda: "3.18.2")

    env = bridge.engine_env()
    assert env["ZCODE_APP_VERSION"] == "3.18.2"
    assert env["ZCODE_CLIENT_LANGUAGE"] == "en-US"


def test_credential_cipher_v2_env_and_sibling_and_permissions(tmp_path, monkeypatch):
    v2_dir = tmp_path / ".zcode" / "v2"
    v2_dir.mkdir(parents=True)
    cred_path = str(v2_dir / "credentials.json")
    monkeypatch.setenv("ZCODE_DATA_BASE_DIR", str(tmp_path))
    monkeypatch.delenv("ZCODE_CREDENTIAL_SECRET_FILE", raising=False)

    # 1) Managed env secret roundtrip
    monkeypatch.setenv("ZCODE_CREDENTIAL_SECRET", "s" * 40)
    req_id = str(uuid.uuid4())
    enc_res = bridge.handle_credential_cipher_request(
        {"requestId": req_id, "operation": "encrypt", "value": "hello-zcode-v2"},
        cred_path=cred_path,
    )
    assert enc_res["requestId"] == req_id
    assert enc_res["result"]["ok"] is True
    ct = enc_res["result"]["value"]
    assert ct.startswith("enc:v2:env:")
    dec_res = bridge.handle_credential_cipher_request(
        {"requestId": req_id, "operation": "decrypt", "value": ct},
        cred_path=cred_path,
    )
    assert dec_res == {"requestId": req_id, "result": {"ok": True, "value": "hello-zcode-v2"}}

    # 2) Dual secret sources must fail closed with CREDENTIAL_SECURE_STORAGE_UNAVAILABLE
    sec_file = tmp_path / "ext-secret"
    sec_file.write_text("x" * 48, encoding="utf-8")
    monkeypatch.setenv("ZCODE_CREDENTIAL_SECRET_FILE", str(sec_file))
    dual_res = bridge.handle_credential_cipher_request(
        {"requestId": req_id, "operation": "decrypt", "value": ct},
        cred_path=cred_path,
    )
    assert dual_res["result"]["ok"] is False
    assert dual_res["result"]["error"]["code"] == "CREDENTIAL_SECURE_STORAGE_UNAVAILABLE"

    # 3) Sibling credential-secret with world-readable mode (0644) must fail on POSIX
    monkeypatch.delenv("ZCODE_CREDENTIAL_SECRET", raising=False)
    monkeypatch.delenv("ZCODE_CREDENTIAL_SECRET_FILE", raising=False)
    sib = v2_dir / "credential-secret"
    sib.write_text("k" * 48, encoding="utf-8")
    os.chmod(str(sib), 0o644)
    bad_perm = bridge.handle_credential_cipher_request(
        {"requestId": req_id, "operation": "encrypt", "value": "abc"},
        cred_path=cred_path,
    )
    assert bad_perm["result"]["ok"] is False
    assert bad_perm["result"]["error"]["code"] == "CREDENTIAL_SECURE_STORAGE_UNAVAILABLE"

    # Fix permissions to 0600 -> succeeds
    os.chmod(str(sib), 0o600)
    ok_perm = bridge.handle_credential_cipher_request(
        {"requestId": req_id, "operation": "encrypt", "value": "abc"},
        cred_path=cred_path,
    )
    assert ok_perm["result"]["ok"] is True


def test_credential_cipher_v2_os_keyring_and_rotation_and_v1_compat(tmp_path, monkeypatch):
    v2_dir = tmp_path / ".zcode" / "v2"
    v2_dir.mkdir(parents=True)
    cred_path = str(v2_dir / "credentials.json")
    source_file = v2_dir / "credential-source"
    source_file.write_text("os\n", encoding="utf-8")
    monkeypatch.setenv("ZCODE_DATA_BASE_DIR", str(tmp_path))
    monkeypatch.delenv("ZCODE_CREDENTIAL_SECRET", raising=False)
    monkeypatch.delenv("ZCODE_CREDENTIAL_SECRET_FILE", raising=False)

    key1 = os.urandom(32)
    key1_b64 = base64.urlsafe_b64encode(key1).rstrip(b"=").decode("ascii")
    calls = []

    def fake_keyring(acct_hex):
        calls.append(acct_hex)
        return current_b64[0]

    current_b64 = [key1_b64]
    monkeypatch.setattr(bridge, "_read_os_keyring_raw", fake_keyring)

    ct1 = bridge.zcode_cipher_encrypt("os-secret-payload", cred_path=cred_path)
    assert ct1.startswith("enc:v2:os:")
    assert bridge.zcode_cipher_decrypt(ct1, cred_path=cred_path) == "os-secret-payload"
    # Second decrypt hits in-memory cache without re-spawning /usr/bin/security
    assert bridge.zcode_cipher_decrypt(ct1, cred_path=cred_path) == "os-secret-payload"
    assert len(calls) == 1

    # Key rotation: new ciphertext with different fp24 evicts stale cached key and reloads
    key2 = os.urandom(32)
    current_b64[0] = base64.urlsafe_b64encode(key2).rstrip(b"=").decode("ascii")
    with bridge._OS_KEYRING_LOCK:
        bridge._OS_KEYRING_CACHE.clear()
    ct2 = bridge.zcode_cipher_encrypt("rotated-payload", cred_path=cred_path)
    # Prime cache with old key1, then decrypt ct2 -> must detect fp mismatch and reload key2
    with bridge._OS_KEYRING_LOCK:
        bridge._OS_KEYRING_CACHE[os.path.abspath(cred_path)] = key1
    assert bridge.zcode_cipher_decrypt(ct2, cred_path=cred_path) == "rotated-payload"

    # Malformed ciphertext returns exact ZCODE_CREDENTIAL_DECRYPT_FAILED error code
    bad = bridge.handle_credential_cipher_request(
        {"requestId": str(uuid.uuid4()), "operation": "decrypt", "value": "enc:v2:bad:123:x"},
        cred_path=cred_path,
    )
    assert bad["result"]["ok"] is False
    assert bad["result"]["error"]["code"] == "ZCODE_CREDENTIAL_DECRYPT_FAILED"
    assert bad["result"]["error"]["message"].startswith("凭据解密失败：")

    # Legacy enc:v1: compatibility
    v1_key = bridge._legacy_v1_keys()[0]
    v1_ct = "enc:v1:" + bridge._aes_gcm_encrypt("legacy-v1-val", v1_key)
    assert bridge.zcode_cipher_decrypt(v1_ct, cred_path=cred_path) == "legacy-v1-val"

    # Invalid credential-source content (anything other than exact "os\n") must fail closed
    source_file.write_text("os", encoding="utf-8")
    bad_src = bridge.handle_credential_cipher_request(
        {"requestId": str(uuid.uuid4()), "operation": "encrypt", "value": "x"},
        cred_path=cred_path,
    )
    assert bad_src["result"]["ok"] is False
    assert bad_src["result"]["error"]["code"] == "CREDENTIAL_SECURE_STORAGE_UNAVAILABLE"

    # enc:v2:env: pinning: when credential-source & sibling credential-secret are absent and
    # credentials.json already holds enc:v2:env:, encrypt/decrypt MUST fail closed and NEVER
    # fall through to OS keyring.
    source_file.unlink()
    (v2_dir / "credentials.json").write_text(
        json.dumps({"zcodejwttoken": "enc:v2:env:0123456789abcdef01234567:aa.bb.cc"}),
        encoding="utf-8",
    )
    with bridge._OS_KEYRING_LOCK:
        bridge._OS_KEYRING_CACHE.clear()
    calls_before = len(calls)
    pinned = bridge.handle_credential_cipher_request(
        {"requestId": str(uuid.uuid4()), "operation": "encrypt", "value": "new-secret"},
        cred_path=cred_path,
    )
    assert pinned["result"]["ok"] is False
    assert pinned["result"]["error"]["code"] == "CREDENTIAL_SECURE_STORAGE_UNAVAILABLE"
    assert len(calls) == calls_before


# ---------------------------------------------------------------- 2. Official MCP & Catalog

def test_official_mcp_auth_headers_tct_schema(tmp_path, monkeypatch):
    v2_dir = tmp_path / ".zcode" / "v2"
    v2_dir.mkdir(parents=True)
    monkeypatch.setenv("ZCODE_DATA_BASE_DIR", str(tmp_path))
    monkeypatch.setenv("ZCODE_CREDENTIAL_SECRET", "m" * 40)
    monkeypatch.delenv("ZCODE_ENDPOINT_ORIGIN", raising=False)
    monkeypatch.delenv("ZCODE_BASE_URL", raising=False)
    monkeypatch.delenv("ZCODE_OFFICIAL_MCP_DEV_TRUSTED_ORIGINS", raising=False)
    cred_path = str(v2_dir / "credentials.json")

    # 1) Untrusted origin -> official_mcp_origin_untrusted
    r1 = bridge.handle_official_mcp_auth_headers({
        "requestId": "r1",
        "pluginId": "web-search",
        "mcpKey": "ws",
        "targetOrigin": "https://attacker.example.com",
    })
    assert r1 == {"ok": False, "reason": "official_mcp_origin_untrusted"}

    # 1b) Dev trusted origins: only loopback http://127.0.0.1|localhost|[::1] allowed (OI)
    monkeypatch.setenv(
        "ZCODE_OFFICIAL_MCP_DEV_TRUSTED_ORIGINS",
        "http://127.0.0.1:4321,http://[::1]:8080,http://evil.example.com:4321,https://127.0.0.1:4321,http://user:pass@127.0.0.1:4321",
    )
    trusted = bridge._trusted_official_mcp_origins()
    assert "http://127.0.0.1:4321" in trusted
    assert "http://[::1]:8080" in trusted
    assert "http://evil.example.com:4321" not in trusted
    assert "https://127.0.0.1:4321" not in trusted
    assert "http://user:pass@127.0.0.1:4321" not in trusted

    # 1c) setting.json zcodeEndpointOrigin & ZCODE_BASE_URL (https only) are trusted
    (v2_dir / "setting.json").write_text(
        json.dumps({"zcodeEndpointOrigin": "https://custom-zcode.example.com/v1"}),
        encoding="utf-8",
    )
    monkeypatch.setenv("ZCODE_BASE_URL", "https://staging-zcode.example.com/api")
    trusted2 = bridge._trusted_official_mcp_origins()
    assert "https://custom-zcode.example.com" in trusted2
    assert "https://staging-zcode.example.com" in trusted2
    monkeypatch.delenv("ZCODE_BASE_URL", raising=False)
    monkeypatch.delenv("ZCODE_OFFICIAL_MCP_DEV_TRUSTED_ORIGINS", raising=False)

    # 2) Trusted origin without credentials -> official_auth_unavailable
    (v2_dir / "credentials.json").write_text("{}", encoding="utf-8")
    r2 = bridge.handle_official_mcp_auth_headers({
        "requestId": "r2",
        "pluginId": "web-search",
        "mcpKey": "ws",
        "targetOrigin": "https://zcode.z.ai",
    })
    assert r2 == {"ok": False, "reason": "official_auth_unavailable"}

    # 3) Trusted origin with personal Coding Plan OAuth + JWT -> full headers
    fake_jwt = _make_fake_zcode_jwt("u1")
    fake_oauth = "oauth-zai-tok-" + uuid.uuid4().hex
    (v2_dir / "setting.json").write_text(json.dumps({
        "providerFamilyDomain": "zai",
        "providerFamilyConnectionSelections": {"zai": {"kind": "individual-coding-plan"}},
    }), encoding="utf-8")
    (v2_dir / "credentials.json").write_text(json.dumps({
        "zcodejwttoken": bridge.zcode_cipher_encrypt(fake_jwt, cred_path=cred_path),
        "oauth:zai:access_token": bridge.zcode_cipher_encrypt(fake_oauth, cred_path=cred_path),
        "oauth:active_provider": "zai",
    }), encoding="utf-8")
    r3 = bridge.handle_official_mcp_auth_headers({
        "requestId": "r3",
        "pluginId": "web-search",
        "mcpKey": "ws",
        "targetOrigin": "https://zcode.z.ai",
    })
    assert r3["ok"] is True
    assert r3["headers"]["Authorization"] == f"Bearer {fake_jwt}"
    assert r3["headers"]["X-Bigmodel-Authorization"] == f"Bearer {fake_oauth}"
    assert r3["headers"]["Bigmodel-Target-Type"] == "PERSONAL"

    # 4) zai team-coding-plan -> wireScope is null (no Bigmodel-Target-Type / Org / Project)
    (v2_dir / "setting.json").write_text(json.dumps({
        "providerFamilyDomain": "zai",
        "providerFamilyConnectionSelections": {
            "zai": {"kind": "team-coding-plan", "organizationId": "org-z", "projectId": "proj-z"},
        },
    }), encoding="utf-8")
    r4 = bridge.handle_official_mcp_auth_headers({
        "requestId": "r4",
        "pluginId": "web-search",
        "mcpKey": "ws",
        "targetOrigin": "https://zcode.z.ai",
    })
    assert r4 == {
        "ok": True,
        "headers": {
            "Authorization": f"Bearer {fake_jwt}",
            "X-Bigmodel-Authorization": f"Bearer {fake_oauth}",
        },
    }

    # 5) bigmodel team-coding-plan -> emits Bigmodel-Target-Type: TEAM + Org + Project
    fake_bm_oauth = "oauth-bm-tok-" + uuid.uuid4().hex
    (v2_dir / "setting.json").write_text(json.dumps({
        "providerFamilyDomain": "bigmodel",
        "providerFamilyConnectionSelections": {
            "bigmodel": {"kind": "team-coding-plan", "organizationId": "org-bm", "projectId": "proj-bm"},
        },
    }), encoding="utf-8")
    (v2_dir / "credentials.json").write_text(json.dumps({
        "zcodejwttoken": bridge.zcode_cipher_encrypt(fake_jwt, cred_path=cred_path),
        "oauth:bigmodel:access_token": bridge.zcode_cipher_encrypt(fake_bm_oauth, cred_path=cred_path),
        "oauth:active_provider": "bigmodel",
    }), encoding="utf-8")
    r5 = bridge.handle_official_mcp_auth_headers({
        "requestId": "r5",
        "pluginId": "web-search",
        "mcpKey": "ws",
        "targetOrigin": "https://zcode.z.ai",
    })
    assert r5 == {
        "ok": True,
        "headers": {
            "Authorization": f"Bearer {fake_jwt}",
            "X-Bigmodel-Authorization": f"Bearer {fake_bm_oauth}",
            "Bigmodel-Target-Type": "TEAM",
            "Bigmodel-Organization": "org-bm",
            "Bigmodel-Project": "proj-bm",
        },
    }

    # 6) Non-coding-plan selection (e.g. start-plan): mcp-server -> official_auth_plan_required,
    #    plugin-command -> Authorization only
    (v2_dir / "setting.json").write_text(json.dumps({
        "providerFamilyDomain": "bigmodel",
        "providerFamilyConnectionSelections": {"bigmodel": {"kind": "start-plan"}},
    }), encoding="utf-8")
    r6_mcp = bridge.handle_official_mcp_auth_headers({
        "requestId": "r6a",
        "pluginId": "web-search",
        "mcpKey": "ws",
        "consumer": "mcp-server",
        "targetOrigin": "https://zcode.z.ai",
    })
    assert r6_mcp == {"ok": False, "reason": "official_auth_plan_required"}
    r6_plug = bridge.handle_official_mcp_auth_headers({
        "requestId": "r6b",
        "pluginId": "web-search",
        "mcpKey": "ws",
        "consumer": "plugin-command",
        "targetOrigin": "https://zcode.z.ai",
    })
    assert r6_plug == {"ok": True, "headers": {"Authorization": f"Bearer {fake_jwt}"}}


def test_builtin_provider_id_mapping_and_hidden_filtering(tmp_path, monkeypatch):
    assert bridge.config_provider_id_for("account:zai-individual-coding-plan", []) == "builtin:zai-coding-plan"
    assert bridge.config_provider_id_for("account:zai-team-coding-plan", []) == "builtin:zai-team-coding-plan"
    assert bridge.config_provider_id_for("account:bigmodel-team-coding-plan", []) == "builtin:bigmodel-team-coding-plan"
    assert bridge.config_provider_id_for("account:zai-start-plan", []) == "builtin:zai-start-plan"
    assert bridge.config_provider_id_for("account:zai-offpeak-idle-plan", []) == "builtin:zai-off-peak"
    assert bridge.config_provider_id_for("account:bigmodel-highspeed-card", []) == "builtin:bigmodel-highspeed"

    # Hidden rules (off-peak / highspeed) must stay entitled=False in account snapshot
    # and never leak into user model catalog; multiple entitled Coding Plans must obey
    # zcode.cjs Win(e) single-current "account-plan" invariant.
    builtin_file = tmp_path / "zcode-builtin.json"
    builtin_file.write_text(json.dumps({
        "revision": 44,
        "config": {
            "providerConfigRules": {
                "providerRules": [
                    {
                        "providerId": "account:zai-individual-coding-plan",
                        "providerName": "Z.AI Individual Coding Plan",
                        "config": {
                            "builtinModelIds": ["GLM-5.3"],
                            "access": {"type": "zhipu-account", "mode": "individual-coding-plan", "accountType": "zai"},
                        },
                    },
                    {
                        "providerId": "account:zai-team-coding-plan",
                        "providerName": "Z.AI Team Coding Plan",
                        "config": {
                            "builtinModelIds": ["GLM-5.3"],
                            "access": {"type": "zhipu-account", "mode": "team-coding-plan", "accountType": "zai"},
                        },
                    },
                    {
                        "providerId": "account:bigmodel-individual-coding-plan",
                        "providerName": "BigModel Individual Coding Plan",
                        "config": {
                            "builtinModelIds": ["GLM-5.3"],
                            "access": {"type": "zhipu-account", "mode": "individual-coding-plan", "accountType": "bigmodel"},
                        },
                    },
                    {
                        "providerId": "account:zai-start-plan",
                        "providerName": "Z.AI Start Plan",
                        "config": {
                            "builtinModelIds": ["GLM-5.3"],
                            "access": {"type": "zhipu-account", "mode": "start-plan", "accountType": "zai"},
                        },
                    },
                    {
                        "providerId": "account:zai-offpeak-idle-plan",
                        "providerName": "Z.AI Idle plan",
                        "config": {
                            "builtinModelIds": ["GLM-5.3"],
                            "visibility": "hidden",
                            "access": {"type": "zhipu-account", "mode": "off-peak", "accountType": "zai"},
                        },
                    },
                ]
            }
        },
    }), encoding="utf-8")
    v2_dir = tmp_path / ".zcode" / "v2"
    v2_dir.mkdir(parents=True)
    (v2_dir / "config.json").write_text("{}", encoding="utf-8")
    (v2_dir / "setting.json").write_text(json.dumps({
        "providerFamilyDomain": "zai",
        "providerFamilyConnectionSelections": {"zai": {"kind": "team-coding-plan"}},
    }), encoding="utf-8")
    monkeypatch.setenv("ZCODE_DATA_BASE_DIR", str(tmp_path))
    monkeypatch.setenv("ZCODE_BUILTIN_PROVIDER_CONFIG_FILE", str(builtin_file))

    multi_entitled = {
        "builtin:zai-coding-plan",
        "builtin:zai-team-coding-plan",
        "builtin:bigmodel-coding-plan",
        "builtin:zai-start-plan",
        "builtin:zai-off-peak",
    }
    snap = bridge.build_account_provider_payload(live_entitled=multi_entitled)
    coding_plan_pids = [
        "account:zai-individual-coding-plan",
        "account:zai-team-coding-plan",
        "account:bigmodel-individual-coding-plan",
    ]
    current_plans = [p for p in coding_plan_pids if snap["states"][p]["current"] is True]
    assert current_plans == ["account:zai-team-coding-plan"]
    for p in coding_plan_pids:
        assert snap["providers"][p]["access"]["entitled"] is True
        assert snap["states"][p]["entitled"] is True
        assert isinstance(snap["states"][p]["current"], bool)
    assert snap["states"]["account:zai-start-plan"]["current"] is True
    assert snap["providers"]["account:zai-offpeak-idle-plan"]["access"]["entitled"] is False
    assert snap["states"]["account:zai-offpeak-idle-plan"]["current"] is False

    # Override via current_account_pid moves the single current=True flag
    snap_bm = bridge.build_account_provider_payload(
        live_entitled=multi_entitled,
        current_account_pid="account:bigmodel-individual-coding-plan",
    )
    assert [p for p in coding_plan_pids if snap_bm["states"][p]["current"] is True] == [
        "account:bigmodel-individual-coding-plan"
    ]

    # Catalog with engine_entries (which only contains the single current=True account-plan)
    # must preserve all other entitled non-hidden account:* plans while hiding off-peak
    engine_entries = [{
        "ref": {"providerId": "account:zai-team-coding-plan", "modelId": "GLM-5.3"},
        "providerLabel": "Z.AI Team Coding Plan",
        "label": "GLM-5.3",
        "contextWindow": 200000,
    }]
    cat = bridge.zcode_model_catalog(engine_entries=engine_entries, live_entitled=multi_entitled)
    mids = [m["modelId"] for m in cat["availableModels"]]
    assert "Z.AI Team Coding Plan/GLM-5.3" in mids
    assert "Z.AI Individual Coding Plan/GLM-5.3" in mids
    assert "BigModel Individual Coding Plan/GLM-5.3" in mids
    assert "Z.AI Start Plan/GLM-5.3" in mids
    assert not any("Idle plan" in m for m in mids)

    # ZCode._set_model must push updated single-current snapshot before session/setModel
    pushed = []
    sent = []

    class _FakeZCode(bridge.ZCode):
        def __init__(self):
            self.next_id = 10
            self.session_id = "sess_1"
            self.lock = threading.Lock()
            self.log = []
            self.account_pushed = True
            self.live_entitled = multi_entitled
            self.current_account_pid = "account:zai-team-coding-plan"

        def engine_call_raw(self, method, params, timeout=10):
            pushed.append((method, params))
            return {"result": {"providerCount": len(params["providers"]), "status": "received"}}

        def _send(self, obj):
            sent.append(obj)
            with self.lock:
                self.log.append({"id": obj["id"], "result": {"ok": True}})

    fzc = _FakeZCode()
    assert fzc._set_model("account:bigmodel-individual-coding-plan", "GLM-5.3", level="medium")
    assert len(pushed) == 1
    assert pushed[0][0] == "provider/updateAccountConfig"
    p_states = pushed[0][1]["states"]
    assert [p for p in coding_plan_pids if p_states[p]["current"] is True] == [
        "account:bigmodel-individual-coding-plan"
    ]
    assert fzc.current_account_pid == "account:bigmodel-individual-coding-plan"
    assert sent and sent[0]["method"] == "session/setModel"

    # AcpServer.handle("session/set_model") must also push single-current snapshot before setModel
    pushed.clear()
    sent.clear()
    srv = bridge.AcpServer()
    srv.zcode = fzc
    srv.catalog = cat
    srv._live_entitled = multi_entitled
    if hasattr(bridge, "_cred_fingerprint"):
        srv._cred_fp = bridge._cred_fingerprint()
    acp_out = []
    srv._send = acp_out.append
    srv.handle({
        "jsonrpc": "2.0",
        "id": 1,
        "method": "session/set_model",
        "params": {"sessionId": "zc-s1", "modelId": "Z.AI Individual Coding Plan/GLM-5.3"},
    })
    assert len(pushed) == 1
    assert pushed[0][0] == "provider/updateAccountConfig"
    p_states2 = pushed[0][1]["states"]
    assert [p for p in coding_plan_pids if p_states2[p]["current"] is True] == [
        "account:zai-individual-coding-plan"
    ]
    assert fzc.current_account_pid == "account:zai-individual-coding-plan"
    assert sent and sent[0]["method"] == "session/setModel"
    assert any(m.get("id") == 1 and "result" in m for m in acp_out)


def test_stale_bigmodel_token_guard_and_user_info_fields(tmp_path, monkeypatch):
    v2_dir = tmp_path / ".zcode" / "v2"
    v2_dir.mkdir(parents=True)
    monkeypatch.setenv("ZCODE_DATA_BASE_DIR", str(tmp_path))
    monkeypatch.setenv("ZCODE_CREDENTIAL_SECRET", "g" * 40)
    cred_path = str(v2_dir / "credentials.json")

    stale_jwt = _make_fake_zcode_jwt("stale")
    (v2_dir / "credentials.json").write_text(json.dumps({
        "zcodejwttoken": bridge.zcode_cipher_encrypt(stale_jwt, cred_path=cred_path),
        "oauth:bigmodel:access_token": bridge.zcode_cipher_encrypt(stale_jwt, cred_path=cred_path),
        "oauth:zai:access_token": bridge.zcode_cipher_encrypt("valid-zai-oauth", cred_path=cred_path),
        "oauth:zai:user_info": bridge.zcode_cipher_encrypt(
            json.dumps({"user_id": "zai-uuid-999"}), cred_path=cred_path),
    }), encoding="utf-8")

    assert bridge._load_oauth_material("bigmodel") is None
    zai_mat = bridge._load_oauth_material("zai")
    assert zai_mat is not None
    assert zai_mat["accountIdentity"] == "zai-uuid-999"


# ---------------------------------------------------------------- 3. ProjectAccessToken & Team Entitlement

def test_project_access_token_exchange_cache_invalidation_and_fail_closed(tmp_path, monkeypatch):
    counts = {"cust": 0, "keys": 0, "mint": 0, "sub_list": 0, "team_sub": 0}
    force_404_key = {"id": None}

    class MockBizHandler(http.server.BaseHTTPRequestHandler):
        def log_message(self, fmt, *args):
            pass

        def _json(self, status, obj):
            body = json.dumps(obj).encode("utf-8")
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def do_GET(self):
            if self.path == "/api/biz/subscription/list":
                counts["sub_list"] += 1
                self._json(200, {
                    "code": 200,
                    "data": [{"productId": "coding-pro", "status": "VALID", "inCurrentPeriod": True}],
                })
            elif self.path == "/api/biz/customer/getCustomerInfo":
                counts["cust"] += 1
                self._json(200, {
                    "code": 200,
                    "data": {
                        "organizations": [{
                            "organizationId": "org_1",
                            "organizationName": "默认机构",
                            "projects": [
                                {"projectId": "proj_personal", "projectName": "默认项目", "projectType": "1"},
                                {"projectId": "proj_team", "projectName": "TeamProj", "projectType": "2"},
                            ],
                        }]
                    },
                })
            elif self.path == "/api/biz/team/subscribe/product/querySubscribeDetail":
                counts["team_sub"] += 1
                org_h = self.headers.get("bigmodel-organization")
                proj_h = self.headers.get("bigmodel-project")
                if org_h == "org_1" and proj_h == "proj_team":
                    self._json(200, {
                        "code": 200,
                        "data": {
                            "hasSubscription": True,
                            "status": "EFFECTIVE",
                            "memberGrantStatus": "VALID",
                            "productId": "team-coding-enterprise",
                        },
                    })
                else:
                    self._json(200, {"code": 200, "data": {"hasSubscription": False}})
            elif self.path == "/api/biz/v1/organization/org_1/projects/proj_personal/api_keys":
                counts["keys"] += 1
                assert self.headers.get("Authorization", "").startswith("Bearer ")
                self._json(200, {
                    "code": 200,
                    "data": [{"name": "zcode-api-key", "apiKey": "kid_personal_01", "keyType": 1}],
                })
            elif self.path == "/api/biz/v1/organization/org_1/projects/proj_team/api_keys":
                counts["keys"] += 1
                assert self.headers.get("bigmodel-organization") == "org_1"
                assert self.headers.get("bigmodel-project") == "proj_team"
                self._json(200, {
                    "code": 200,
                    "data": [{"name": "zcode-team-api-key", "apiKey": "kid_team_02", "keyType": 2}],
                })
            else:
                self._json(404, {"code": 404})

        def do_POST(self):
            if self.path.endswith("/access_tokens"):
                counts["mint"] += 1
                kid = self.path.split("/api_keys/")[1].split("/")[0]
                if force_404_key["id"] == kid:
                    force_404_key["id"] = None
                    self._json(404, {"code": 404, "msg": "deleted key"})
                    return
                pat = _make_fake_pat(api_key_id=kid, jti=f"mint-{counts['mint']}")
                # Intentionally omit "enable" field to match real Z.AI production response!
                self._json(200, {
                    "code": 200,
                    "data": {
                        "accessToken": pat,
                        "tokenType": "Bearer",
                        "expiresIn": 18000,
                        "expiresAt": int(time.time()) + 18000,
                    },
                })
            else:
                self._json(404, {"code": 404})

    srv = socketserver.TCPServer(("127.0.0.1", 0), MockBizHandler)
    port = srv.server_address[1]
    t = threading.Thread(target=srv.serve_forever, daemon=True)
    t.start()
    try:
        v2_dir = tmp_path / ".zcode" / "v2"
        v2_dir.mkdir(parents=True)
        cred_path = str(v2_dir / "credentials.json")
        monkeypatch.setenv("ZCODE_DATA_BASE_DIR", str(tmp_path))
        monkeypatch.setenv("ZCODE_CREDENTIAL_SECRET", "p" * 40)
        monkeypatch.setenv("ZAI_BUSINESS_BASE_URL", f"http://127.0.0.1:{port}")
        monkeypatch.setenv("BIGMODEL_API_BASE_URL", f"http://127.0.0.1:{port}")
        monkeypatch.setattr(bridge, "live_start_plan_active", lambda fam: False)

        fake_jwt = _make_fake_zcode_jwt("u_pat")
        (v2_dir / "setting.json").write_text(json.dumps({
            "providerFamilyDomain": "bigmodel",
            "providerFamilyConnectionSelections": {
                "bigmodel": {
                    "kind": "team-coding-plan",
                    "organizationId": "org_1",
                    "projectId": "proj_team",
                    "productId": "team-coding-enterprise",
                },
                "zai": {"kind": "individual-coding-plan"},
            },
        }), encoding="utf-8")
        (v2_dir / "credentials.json").write_text(json.dumps({
            "zcodejwttoken": bridge.zcode_cipher_encrypt(fake_jwt, cred_path=cred_path),
            "oauth:zai:access_token": bridge.zcode_cipher_encrypt("zai-oauth-tok", cred_path=cred_path),
            "oauth:zai:user_info": bridge.zcode_cipher_encrypt(json.dumps({"user_id": "zai_u1"}), cred_path=cred_path),
            "oauth:bigmodel:access_token": bridge.zcode_cipher_encrypt("bm-oauth-tok", cred_path=cred_path),
            "oauth:bigmodel:user_info": bridge.zcode_cipher_encrypt(json.dumps({"id": "bm_u2"}), cred_path=cred_path),
            "oauth:active_provider": "zai",
        }), encoding="utf-8")

        # 1) Live entitlement detects BOTH individual-coding-plan AND team-coding-plan
        ent = bridge.live_entitled_plans()
        assert "builtin:zai-coding-plan" in ent
        assert "builtin:bigmodel-team-coding-plan" in ent

        # 2) Individual Coding Plan mints PAT + caches second call
        auth1 = bridge.coding_plan_request_auth("account:zai-individual-coding-plan", {"reason": "model-request"})
        assert auth1 is not None
        assert auth1["apiKeyId"] == "kid_personal_01"
        assert len(auth1["accountScope"]) == 64
        kind1, parsed_kid = bridge._parse_signing_credential(auth1["apiKey"])
        assert (kind1, parsed_kid) == ("project_access", "kid_personal_01")
        mint_after_1 = counts["mint"]

        auth2 = bridge.coding_plan_request_auth("account:zai-individual-coding-plan", {"reason": "model-request"})
        assert auth2["apiKey"] == auth1["apiKey"]
        assert counts["mint"] == mint_after_1  # Cached!

        # 3) rejectedProjectTokenFingerprint invalidates cached PAT and mints a fresh PAT
        fp1 = hashlib.sha256(auth1["apiKey"].encode("utf-8")).hexdigest()
        auth3 = bridge.coding_plan_request_auth("account:zai-individual-coding-plan", {
            "reason": "model-request",
            "expectedAccountScope": auth1["accountScope"],
            "rejectedProjectTokenFingerprint": fp1,
        })
        assert auth3 is not None
        assert auth3["apiKey"] != auth1["apiKey"]
        assert counts["mint"] == mint_after_1 + 1

        # 4) Scope guards: rejected_fp without expectedAccountScope or wrong scope fails closed
        assert bridge.coding_plan_request_auth("account:zai-individual-coding-plan", {
            "rejectedProjectTokenFingerprint": fp1,
        }) is None
        assert bridge.coding_plan_request_auth("account:zai-individual-coding-plan", {
            "expectedAccountScope": "0" * 64,
        }) is None

        # 5) Stale 404 location in cache recovers automatically by re-discovering org/project/key
        stale_key = ("http://127.0.0.1:" + str(port), "zai", "zai_u1", None)
        with bridge._PAT_LOCK:
            bridge._PROJECT_LOCATION_CACHE[stale_key] = {
                "organizationId": "org_1",
                "projectId": "proj_personal",
                "apiKeyId": "kid_deleted_stale",
            }
        force_404_key["id"] = "kid_deleted_stale"
        auth_rec = bridge.coding_plan_request_auth("account:zai-individual-coding-plan", {"reason": "model-request"})
        assert auth_rec is not None
        assert auth_rec["apiKeyId"] == "kid_personal_01"

        # 6) Team Coding Plan mints PAT using team headers & keyType=2
        team_auth = bridge.coding_plan_request_auth("account:bigmodel-team-coding-plan", {"reason": "model-request"})
        assert team_auth is not None
        assert team_auth["apiKeyId"] == "kid_team_02"
        assert len(team_auth["accountScope"]) == 64

        # 7) Fail-closed guard: when OAuth tokens are absent and ONLY zcodejwttoken exists,
        #    individual/team coding-plan MUST return None (never zcodejwttoken!), while start-plan gets zcodejwttoken.
        (v2_dir / "credentials.json").write_text(json.dumps({
            "zcodejwttoken": bridge.zcode_cipher_encrypt(fake_jwt, cred_path=cred_path),
            "oauth:active_provider": "zai",
        }), encoding="utf-8")
        bridge.clear_project_token_caches()
        assert bridge.coding_plan_request_auth("account:zai-individual-coding-plan") is None
        assert bridge.coding_plan_request_auth("account:bigmodel-team-coding-plan") is None
        assert bridge.coding_plan_request_auth("account:zai-start-plan") == {"apiKey": fake_jwt}
    finally:
        srv.shutdown()
        srv.server_close()


# ---------------------------------------------------------------- 4. Live Engine 4-Layer E2E Proof

def _generate_localhost_tls(cert_path, key_path):
    priv = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    subject = issuer = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "127.0.0.1")])
    now = datetime.datetime.now(datetime.timezone.utc)
    cert = (
        x509.CertificateBuilder()
        .subject_name(subject)
        .issuer_name(issuer)
        .public_key(priv.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now - datetime.timedelta(minutes=5))
        .not_valid_after(now + datetime.timedelta(days=1))
        .add_extension(
            x509.SubjectAlternativeName([
                x509.IPAddress(ipaddress.IPv4Address("127.0.0.1")),
                x509.DNSName("localhost"),
            ]),
            critical=False,
        )
        .add_extension(x509.BasicConstraints(ca=True, path_length=None), critical=True)
        .sign(priv, hashes.SHA256())
    )
    with open(cert_path, "wb") as f:
        f.write(cert.public_bytes(serialization.Encoding.PEM))
    with open(key_path, "wb") as f:
        f.write(priv.private_bytes(
            serialization.Encoding.PEM,
            serialization.PrivateFormat.TraditionalOpenSSL,
            serialization.NoEncryption(),
        ))


def _hkdf_sha256(ikm_bytes, info_bytes):
    return HKDF(
        algorithm=hashes.SHA256(),
        length=32,
        salt=b"WD_CLIENT_SIGN_KDF_SALT",
        info=info_bytes,
    ).derive(ikm_bytes)


@pytest.mark.skipif(
    os.environ.get("ZCODE_ACP_SKIP_ENGINE") == "1" or not os.path.isfile(bridge.ZC_CJS),
    reason="Requires local ZCode engine (zcode.cjs)",
)
def test_live_engine_four_layer_150_quota_chain_e2e(tmp_path, monkeypatch):
    """驱动真实 zcode.cjs 跑通 Coding Plan 150% 额度四层链路，并在本地 TLS 网关数学验签。"""
    cert_pem = str(tmp_path / "mock-ca.pem")
    key_pem = str(tmp_path / "mock-ca.key")
    _generate_localhost_tls(cert_pem, key_pem)

    ed_priv = ed25519.Ed25519PrivateKey.generate()
    ed_pub = ed_priv.public_key()
    pkcs8_b64 = base64.b64encode(ed_priv.private_bytes(
        serialization.Encoding.DER,
        serialization.PrivateFormat.PKCS8,
        serialization.NoEncryption(),
    ))

    captured = {
        "agent_configs_headers": None,
        "handshake_body": None,
        "ultra_request": None,
    }
    expected_zcode_jwt = _make_fake_zcode_jwt("live_e2e_user")

    class QuietTLSServer(socketserver.ThreadingMixIn, http.server.HTTPServer):
        daemon_threads = True

        def handle_error(self, request, client_address):
            pass

    class MockZCodeCloudHandler(http.server.BaseHTTPRequestHandler):
        def log_message(self, fmt, *args):
            pass

        def _json(self, status, obj):
            body = json.dumps(obj).encode("utf-8")
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def do_GET(self):
            origin = f"https://127.0.0.1:{self.server.server_port}"
            if self.path.startswith("/api/v1/agent/configs"):
                captured["agent_configs_headers"] = dict(self.headers.items())
                self._json(200, {
                    "code": 0,
                    "data": {
                        "proxyEndpoint": {
                            "mapping": [{
                                "from": f"{origin}/api/anthropic/v1/messages",
                                "to": f"{origin}/api/v1/ultra-zai/anthropic/v1/messages",
                                "zcodeAuthorizationHeader": "X-ZCode-Authorization",
                            }]
                        }
                    },
                })
            elif self.path == "/api/biz/customer/getCustomerInfo":
                self._json(200, {
                    "code": 200,
                    "data": {
                        "organizations": [{
                            "organizationId": "org_live",
                            "organizationName": "默认机构",
                            "projects": [{"projectId": "proj_live", "projectName": "默认项目", "projectType": "1"}],
                        }]
                    },
                })
            elif self.path == "/api/biz/v1/organization/org_live/projects/proj_live/api_keys":
                self._json(200, {
                    "code": 200,
                    "data": [{"name": "zcode-api-key", "apiKey": "kid_live_e2e", "keyType": 1}],
                })
            else:
                self._json(404, {"code": 404})

        def do_POST(self):
            length = int(self.headers.get("Content-Length") or "0")
            raw_body = self.rfile.read(length) if length > 0 else b""
            if self.path.endswith("/access_tokens"):
                pat = _make_fake_pat(api_key_id="kid_live_e2e")
                self._json(200, {
                    "code": 200,
                    "data": {
                        "accessToken": pat,
                        "tokenType": "Bearer",
                        "expiresIn": 18000,
                        "expiresAt": int(time.time()) + 18000,
                    },
                })
            elif self.path == "/api/paas/c1f3a7e2/v2/client":
                body = json.loads(raw_body.decode("utf-8"))
                captured["handshake_body"] = body
                cred = body["accessToken"]
                # Verify zcode.cjs handshake HMAC!
                mac_key = _hkdf_sha256(cred.encode("utf-8"), b"getSignKey_hmac")
                msg = f"get_sign_key\nkid_live_e2e\n{body['ts']}\n{body['nonce']}".encode("utf-8")
                expected_mac = base64.b64encode(hmac.new(mac_key, msg, hashlib.sha256).digest()).decode("ascii")
                assert body["sig"] == expected_mac
                # Encrypt Ed25519 PKCS#8 private key per lfo()
                aes_key = _hkdf_sha256(cred.encode("utf-8"), b"ed25519_priv")
                iv = os.urandom(12)
                enc = AESGCM(aes_key).encrypt(iv, pkcs8_b64, b"kid_live_e2e")
                priv_cipher = base64.b64encode(iv + enc).decode("ascii")
                self._json(200, {"code": 200, "data": {"privateCipher": priv_cipher}})
            elif self.path.startswith("/api/v1/ultra-zai/anthropic/v1/messages"):
                captured["ultra_request"] = {
                    "path": self.path,
                    "headers": {k.lower(): v for k, v in self.headers.items()},
                }
                sse = (
                    'event: message_start\n'
                    'data: {"type":"message_start","message":{"id":"msg_e2e","type":"message","role":"assistant","model":"GLM-5.3","content":[],"stop_reason":null,"stop_sequence":null,"usage":{"input_tokens":8,"output_tokens":1}}}\n\n'
                    'event: content_block_start\n'
                    'data: {"type":"content_block_start","index":0,"content_block":{"type":"text","text":""}}\n\n'
                    'event: content_block_delta\n'
                    'data: {"type":"content_block_delta","index":0,"delta":{"type":"text_delta","text":"QUOTA_150_VERIFIED"}}\n\n'
                    'event: content_block_stop\n'
                    'data: {"type":"content_block_stop","index":0}\n\n'
                    'event: message_delta\n'
                    'data: {"type":"message_delta","delta":{"stop_reason":"end_turn","stop_sequence":null},"usage":{"output_tokens":4}}\n\n'
                    'event: message_stop\n'
                    'data: {"type":"message_stop"}\n\n'
                ).encode("utf-8")
                self.send_response(200)
                self.send_header("Content-Type", "text/event-stream; charset=utf-8")
                self.send_header("Cache-Control", "no-cache")
                self.send_header("Content-Length", str(len(sse)))
                self.end_headers()
                self.wfile.write(sse)
            else:
                self._json(404, {"code": 404, "path": self.path})

    httpd = QuietTLSServer(("127.0.0.1", 0), MockZCodeCloudHandler)
    ssl_ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    ssl_ctx.load_cert_chain(certfile=cert_pem, keyfile=key_pem)
    httpd.socket = ssl_ctx.wrap_socket(httpd.socket, server_side=True)
    port = httpd.server_address[1]
    origin = f"https://127.0.0.1:{port}"
    threading.Thread(target=httpd.serve_forever, daemon=True).start()

    # Prepare isolated ZCode data dir + patched builtin table pointing account:zai-individual-coding-plan at our TLS server
    real_builtin = bridge.resolve_builtin_table_path()
    assert real_builtin and os.path.isfile(real_builtin)
    with open(real_builtin, encoding="utf-8") as f:
        btable = json.load(f)
    for r in btable["config"]["providerConfigRules"]["providerRules"]:
        if r.get("providerId") == "account:zai-individual-coding-plan":
            r["config"]["api"]["baseUrl"] = f"{origin}/api/anthropic"

    custom_builtin = tmp_path / "zcode-builtin-e2e.json"
    custom_builtin.write_text(json.dumps(btable), encoding="utf-8")
    custom_personal = tmp_path / "provider_config.json"
    custom_personal.write_text(json.dumps({
        "schemaVersion": 1,
        "config": {
            "providerConfigRules": {"providerRules": []},
            "modelConfigRules": {"providerModelRules": [], "manualProviderModelRules": []},
        },
    }), encoding="utf-8")

    data_base = tmp_path / "data"
    v2_dir = data_base / ".zcode" / "v2"
    cli_dir = data_base / ".zcode" / "cli"
    v2_dir.mkdir(parents=True)
    cli_dir.mkdir(parents=True)
    cred_path = str(v2_dir / "credentials.json")

    monkeypatch.setenv("HOME", str(data_base))
    monkeypatch.setenv("ZCODE_DATA_BASE_DIR", str(data_base))
    monkeypatch.setenv("ZCODE_CREDENTIAL_SECRET", "e" * 42)
    monkeypatch.setenv("ZCODE_BUILTIN_PROVIDER_CONFIG_FILE", str(custom_builtin))
    monkeypatch.setenv("ZCODE_PERSONAL_PROVIDER_CONFIG_FILE", str(custom_personal))
    monkeypatch.setenv("ZCODE_BASE_URL", origin)
    monkeypatch.setenv("ZCODE_ENDPOINT_ORIGIN", origin)
    monkeypatch.setenv("ZAI_BUSINESS_BASE_URL", origin)
    monkeypatch.setenv("NODE_EXTRA_CA_CERTS", cert_pem)

    (v2_dir / "setting.json").write_text(json.dumps({
        "uiLocale": "zh-CN",
        "providerFamilyDomain": "zai",
        "providerFamilyConnectionSelections": {"zai": {"kind": "individual-coding-plan"}},
    }), encoding="utf-8")
    (v2_dir / "credentials.json").write_text(json.dumps({
        "zcodejwttoken": bridge.zcode_cipher_encrypt(expected_zcode_jwt, cred_path=cred_path),
        "oauth:zai:access_token": bridge.zcode_cipher_encrypt("zai-e2e-oauth-tok", cred_path=cred_path),
        "oauth:zai:user_info": bridge.zcode_cipher_encrypt(json.dumps({"user_id": "u_e2e"}), cred_path=cred_path),
        "oauth:active_provider": "zai",
    }), encoding="utf-8")

    app_ver = bridge.zcode_app_version()
    work_dir = tmp_path / "ws"
    work_dir.mkdir()
    zc = bridge.ZCode(str(work_dir))
    try:
        zc.live_entitled = {"builtin:zai-coding-plan"}
        zc.enable_network_capture("cap-e2e-1")
        sid = zc.create_session()
        assert sid
        assert zc._set_model("account:zai-individual-coding-plan", "GLM-5.3", level="low")
        chunks = []
        reply = zc.send("ping", on_update=chunks.append, timeout=30)
        assert "QUOTA_150_VERIFIED" in reply or "QUOTA_150_VERIFIED" in "".join(chunks)

        # Wait up to 1s for 250ms networkCapture flush
        deadline = time.time() + 1.5
        net_urls = []
        while time.time() < deadline:
            with zc.lock:
                for m in zc.log:
                    if m.get("method") == "process/networkRequests":
                        for rec in (m.get("params") or {}).get("records") or []:
                            net_urls.append(rec.get("url") or "")
            if any("/api/v1/ultra-zai/anthropic/v1/messages" in u for u in net_urls):
                break
            time.sleep(0.1)

        # Assert all 4 layers on the captured real zcode.cjs outbound request!
        assert captured["handshake_body"] is not None, "Layer 2 handshake never fired"
        assert "accessToken" in captured["handshake_body"], "Layer 1 PAT was not used as accessToken"
        assert captured["agent_configs_headers"] is not None, "Layer 3 /api/v1/agent/configs never queried"
        cfg_hdrs = {k.lower(): v for k, v in captured["agent_configs_headers"].items()}
        assert cfg_hdrs.get("x-api-key-id") == "kid_live_e2e"

        ultra = captured["ultra_request"]
        assert ultra is not None, "Layer 3 rewritten /api/v1/ultra-zai request never arrived"
        h = ultra["headers"]

        # Layer 4: Desktop source & version headers
        assert (h.get("user-agent") or "").startswith(f"ZCode/{app_ver}")
        assert h.get("x-zcode-app-version") == app_ver
        assert h.get("x-title") == "Z Code@electron"
        assert h.get("x-client-language") == "zh-CN"

        # Layer 3: Ultra routing + decrypted zcodejwttoken via requestCredentialCipher
        assert h.get("x-zcode-authorization") == f"Bearer {expected_zcode_jwt}"

        # Layer 1: Short-lived ProjectAccessToken carried as Anthropic API key
        pat_kind, pat_kid = bridge._parse_signing_credential(h.get("x-api-key") or "")
        assert (pat_kind, pat_kid) == ("project_access", "kid_live_e2e")

        # Layer 2: Mathematical verification of 8-bit SHA-256 PoW + Ed25519 signature!
        assert h.get("x-app-id") == "zcode"
        assert h.get("x-client-version") == app_ver
        ts = h["x-client-ts"]
        nonce = h["x-client-nonce"]
        sess_hdr = h["x-session-id"]
        pow_hex = h["x-client-pow"]
        sig_b64 = h["x-client-sig"]

        seed_prefix = hashlib.sha256(f"kid_live_e2e\nzcode\n{sess_hdr}\n{ts}".encode("utf-8")).hexdigest()[:32]
        pow_digest = hashlib.sha256(f"{seed_prefix}\n{pow_hex}".encode("utf-8")).digest()
        assert pow_digest[0] == 0, "8-bit SHA-256 PoW first byte must be 0x00"

        ed_pub.verify(
            base64.b64decode(sig_b64),
            f"kid_live_e2e\n{ts}\n{app_ver}\n{sess_hdr}\n{nonce}".encode("utf-8"),
        )
        assert any("/api/v1/ultra-zai/anthropic/v1/messages" in u for u in net_urls)
    finally:
        zc.proc.kill()
        httpd.shutdown()
        httpd.server_close()


@pytest.mark.skipif(
    os.environ.get("ZCODE_ACP_SKIP_ENGINE") == "1" or not os.path.isfile(bridge.ZC_CJS),
    reason="Requires local ZCode engine (zcode.cjs)",
)
def test_live_engine_coding_plan_api_key_channel_four_layers(tmp_path, monkeypatch):
    """驱动真实 zcode.cjs 验证另一条 150% 额度路径：Personal Coding Plan API Key (<id>.<secret>)
    模板渠道（bigmodel-api），验证 handshakeField='apiKey' + /api/v1/ultra 路由改写 +
    X-ZCode-Authorization + Ed25519/PoW 签名 + ZCode/3.18.2 头。"""
    cert_pem = str(tmp_path / "mock-ak-ca.pem")
    key_pem = str(tmp_path / "mock-ak-ca.key")
    _generate_localhost_tls(cert_pem, key_pem)

    ed_priv = ed25519.Ed25519PrivateKey.generate()
    ed_pub = ed_priv.public_key()
    pkcs8_b64 = base64.b64encode(ed_priv.private_bytes(
        serialization.Encoding.DER,
        serialization.PrivateFormat.PKCS8,
        serialization.NoEncryption(),
    ))

    ak_id = "akid_99887766"
    ak_secret = "sec_" + uuid.uuid4().hex[:16]
    full_api_key = f"{ak_id}.{ak_secret}"
    expected_zcode_jwt = _make_fake_zcode_jwt("ak_user")
    captured = {"handshake": None, "ultra": None}

    class QuietTLSServer(socketserver.ThreadingMixIn, http.server.HTTPServer):
        daemon_threads = True

        def handle_error(self, request, client_address):
            pass

    class MockBigModelUltraHandler(http.server.BaseHTTPRequestHandler):
        def log_message(self, fmt, *args):
            pass

        def _json(self, status, obj):
            body = json.dumps(obj).encode("utf-8")
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def do_GET(self):
            origin = f"https://127.0.0.1:{self.server.server_port}"
            if self.path.startswith("/api/v1/agent/configs"):
                self._json(200, {
                    "code": 0,
                    "data": {
                        "proxyEndpoint": {
                            "mapping": [{
                                "from": f"{origin}/api/anthropic/v1/messages",
                                "to": f"{origin}/api/v1/ultra/anthropic/v1/messages",
                                "zcodeAuthorizationHeader": "X-ZCode-Authorization",
                            }]
                        }
                    },
                })
            else:
                self._json(404, {"code": 404})

        def do_POST(self):
            length = int(self.headers.get("Content-Length") or "0")
            raw_body = self.rfile.read(length) if length > 0 else b""
            if self.path == "/api/paas/c1f3a7e2/v2/client":
                body = json.loads(raw_body.decode("utf-8"))
                captured["handshake"] = body
                assert body.get("apiKey") == full_api_key
                mac_key = _hkdf_sha256(ak_secret.encode("utf-8"), b"getSignKey_hmac")
                msg = f"get_sign_key\n{ak_id}\n{body['ts']}\n{body['nonce']}".encode("utf-8")
                expected_mac = base64.b64encode(hmac.new(mac_key, msg, hashlib.sha256).digest()).decode("ascii")
                assert body["sig"] == expected_mac
                aes_key = _hkdf_sha256(ak_secret.encode("utf-8"), b"ed25519_priv")
                iv = os.urandom(12)
                enc = AESGCM(aes_key).encrypt(iv, pkcs8_b64, ak_id.encode("utf-8"))
                self._json(200, {"code": 200, "data": {"privateCipher": base64.b64encode(iv + enc).decode("ascii")}})
            elif self.path.startswith("/api/v1/ultra/anthropic/v1/messages"):
                captured["ultra"] = {
                    "path": self.path,
                    "headers": {k.lower(): v for k, v in self.headers.items()},
                }
                sse = (
                    'event: message_start\n'
                    'data: {"type":"message_start","message":{"id":"msg_ak","type":"message","role":"assistant","model":"GLM-5.3","content":[],"stop_reason":null,"stop_sequence":null,"usage":{"input_tokens":6,"output_tokens":1}}}\n\n'
                    'event: content_block_start\n'
                    'data: {"type":"content_block_start","index":0,"content_block":{"type":"text","text":""}}\n\n'
                    'event: content_block_delta\n'
                    'data: {"type":"content_block_delta","index":0,"delta":{"type":"text_delta","text":"API_KEY_150_OK"}}\n\n'
                    'event: content_block_stop\n'
                    'data: {"type":"content_block_stop","index":0}\n\n'
                    'event: message_delta\n'
                    'data: {"type":"message_delta","delta":{"stop_reason":"end_turn","stop_sequence":null},"usage":{"output_tokens":3}}\n\n'
                    'event: message_stop\n'
                    'data: {"type":"message_stop"}\n\n'
                ).encode("utf-8")
                self.send_response(200)
                self.send_header("Content-Type", "text/event-stream; charset=utf-8")
                self.send_header("Content-Length", str(len(sse)))
                self.end_headers()
                self.wfile.write(sse)
            else:
                self._json(404, {"code": 404})

    httpd = QuietTLSServer(("127.0.0.1", 0), MockBigModelUltraHandler)
    ssl_ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    ssl_ctx.load_cert_chain(certfile=cert_pem, keyfile=key_pem)
    httpd.socket = ssl_ctx.wrap_socket(httpd.socket, server_side=True)
    port = httpd.server_address[1]
    origin = f"https://127.0.0.1:{port}"
    threading.Thread(target=httpd.serve_forever, daemon=True).start()

    real_builtin = bridge.resolve_builtin_table_path()
    with open(real_builtin, encoding="utf-8") as f:
        btable = json.load(f)
    for tr in btable["config"]["providerConfigRules"]["templateRules"]:
        if tr.get("templateId") == "bigmodel-api":
            tr["config"]["api"]["baseUrl"] = f"{origin}/api/anthropic"

    custom_builtin = tmp_path / "zcode-builtin-ak.json"
    custom_builtin.write_text(json.dumps(btable), encoding="utf-8")
    custom_personal = tmp_path / "provider_config_ak.json"
    custom_personal.write_text(json.dumps({
        "schemaVersion": 1,
        "config": {
            "providerConfigRules": {
                "providerRules": [{
                    "providerId": "my-bm-coding",
                    "templateId": "bigmodel-api",
                    "providerName": "BigModel Coding Key",
                    "enabled": True,
                    "config": {
                        "group": "standard-personal",
                        "access": {"type": "zhipu-coding-plan-api-key", "apiKey": full_api_key},
                        "personalModelIds": [],
                        "modelOrder": ["GLM-5.3"],
                    },
                }]
            },
            "modelConfigRules": {"providerModelRules": [], "manualProviderModelRules": []},
        },
    }), encoding="utf-8")

    data_base = tmp_path / "data_ak"
    v2_dir = data_base / ".zcode" / "v2"
    (data_base / ".zcode" / "cli").mkdir(parents=True)
    v2_dir.mkdir(parents=True)
    cred_path = str(v2_dir / "credentials.json")

    monkeypatch.setenv("HOME", str(data_base))
    monkeypatch.setenv("ZCODE_DATA_BASE_DIR", str(data_base))
    monkeypatch.setenv("ZCODE_CREDENTIAL_SECRET", "a" * 42)
    monkeypatch.setenv("ZCODE_BUILTIN_PROVIDER_CONFIG_FILE", str(custom_builtin))
    monkeypatch.setenv("ZCODE_PERSONAL_PROVIDER_CONFIG_FILE", str(custom_personal))
    monkeypatch.setenv("ZCODE_BASE_URL", origin)
    monkeypatch.setenv("ZCODE_ENDPOINT_ORIGIN", origin)
    monkeypatch.setenv("NODE_EXTRA_CA_CERTS", cert_pem)

    (v2_dir / "credentials.json").write_text(json.dumps({
        "zcodejwttoken": bridge.zcode_cipher_encrypt(expected_zcode_jwt, cred_path=cred_path),
    }), encoding="utf-8")

    app_ver = bridge.zcode_app_version()
    work_dir = tmp_path / "ws_ak"
    work_dir.mkdir()
    zc = bridge.ZCode(str(work_dir))
    try:
        zc.live_entitled = set()
        sid = zc.create_session()
        assert sid
        assert zc._set_model("my-bm-coding", "GLM-5.3", level="low")
        reply = zc.send("ping", timeout=30)
        assert "API_KEY_150_OK" in reply

        assert captured["handshake"] is not None
        assert captured["handshake"].get("apiKey") == full_api_key
        ultra = captured["ultra"]
        assert ultra is not None
        h = ultra["headers"]
        assert (h.get("user-agent") or "").startswith(f"ZCode/{app_ver}")
        assert h.get("x-zcode-app-version") == app_ver
        assert h.get("x-zcode-authorization") == f"Bearer {expected_zcode_jwt}"
        assert h.get("x-api-key") == full_api_key
        assert h.get("x-app-id") == "zcode"
        assert h.get("x-client-version") == app_ver
        ed_pub.verify(
            base64.b64decode(h["x-client-sig"]),
            f"{ak_id}\n{h['x-client-ts']}\n{app_ver}\n{h['x-session-id']}\n{h['x-client-nonce']}".encode("utf-8"),
        )
    finally:
        zc.proc.kill()
        httpd.shutdown()
        httpd.server_close()
