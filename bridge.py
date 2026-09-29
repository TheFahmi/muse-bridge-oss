#!/usr/bin/env python3
"""Muse bridge: OpenAI-compatible endpoint on VPS localhost.
9Router 'Muse' provider -> http://127.0.0.1:8765/v1 (or https://<domain-kamu>/v1)
Requests are queued; the Muse agent (sandbox) answers via push trigger.

v6: multi-account. 1 Muse account = 1 API key.
    - api_keys.json: {keys: {<id>: {id, name, key, quota, used, enabled,
      created_at, last_used_at}}}
    - Per-key real subscription quota: POST /v1/real-quota (Bearer key) stores
      real_quotas/<id>.json, written by a reporter running wherever that
      account is logged in. GET /v1/real-quota returns the caller's key quota.
    - Enforcement: manual per-key quota AND real weekly-limit exhaustion
      (used_pct >= 100 with fresh data) both yield 429.
    - Admin dashboard at /admin/ (Bearer admin_secret): list/create/update/
      delete keys, reset usage, per-account real quota view.
    - Legacy single real_quota.json is used as fallback for GET.

v5: GET /v1/real-quota serves the agent's real Muse subscription quota.
v4: API key auth (like Command Code). /health stays open.
"""
import json, os, time, uuid, socket, threading, hmac, secrets
import mimetypes
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

BASE = "/home/ubuntu/muse-bridge/queue"
PENDING = f"{BASE}/pending"
DONE = f"{BASE}/done"
MAX_PENDING = 5
WAIT_SECS = 240
KEEPALIVE_SECS = 15
API_KEYS_FILE = "/home/ubuntu/muse-bridge/api_keys.json"
REAL_QUOTA_DIR = "/home/ubuntu/muse-bridge/real_quotas"
LEGACY_REAL_QUOTA_FILE = "/home/ubuntu/muse-bridge/real_quota.json"
# Optional push trigger: bridge POSTs new request ids here so the answer
# worker can wake up instantly instead of polling. Empty = polling only.
PUSH_URL = os.environ.get("PUSH_URL", "").strip()
PUSH_SECRET_FILE = "/home/ubuntu/muse-bridge/push_secret"
ADMIN_SECRET_FILE = "/home/ubuntu/muse-bridge/admin_secret"
ADMIN_HTML_FILE = "/home/ubuntu/muse-bridge/admin.html"
ADMIN_UI_DIR = "/home/ubuntu/muse-bridge/admin-next/out"
REPORT_QUOTA_FILE = "/home/ubuntu/muse-bridge/report_quota.py"
LANDING_FILE = "/home/ubuntu/muse-bridge/landing.html"
REAL_QUOTA_MAX_AGE = 3 * 3600  # s; older data is treated as unknown (fail open)


_keys_lock = threading.Lock()


def _load_keys():
    """Load API keys: {id: {id, name, key, quota, used, enabled, ...}}."""
    with _keys_lock:
        try:
            with open(API_KEYS_FILE) as f:
                data = json.load(f)
            keys = data.get("keys", {})
            # migrate legacy {token: {...}} format on the fly (read-only view)
            if keys and not all(isinstance(v, dict) and "key" in v for v in keys.values()):
                keys = {}
            return keys if isinstance(keys, dict) else {}
        except Exception:
            return {}


def _save_keys(keys):
    with _keys_lock:
        tmp = API_KEYS_FILE + ".tmp"
        with open(tmp, "w") as f:
            json.dump({"keys": keys}, f)
        os.replace(tmp, API_KEYS_FILE)


def _find_key_by_token(token):
    if not token:
        return None
    for rec in _load_keys().values():
        if isinstance(rec, dict) and hmac.compare_digest(token, str(rec.get("key", ""))):
            return rec
    return None


def _push_notify(rid):
    """Tell the sandbox a new request is queued (push, not poll).

    Fire-and-forget in a daemon thread: never blocks the request path.
    The sandbox's push receiver drops a trigger file; a hook wakes the
    answer worker within seconds.
    """
    def _do():
        if not PUSH_URL:
            return
        try:
            with open(PUSH_SECRET_FILE) as f:
                secret = f.read().strip()
        except Exception:
            return
        if not secret:
            return
        body = json.dumps({"id": rid}).encode()
        for attempt in range(3):
            try:
                rq = urllib.request.Request(
                    PUSH_URL, data=body, method="POST",
                    headers={"Content-Type": "application/json",
                             "Authorization": "Bearer " + secret})
                with urllib.request.urlopen(rq, timeout=5) as r:
                    if r.status == 200:
                        return
            except Exception:
                time.sleep(1 + attempt * 2)

    threading.Thread(target=_do, daemon=True).start()


def _real_quota_path(key_id):
    return os.path.join(REAL_QUOTA_DIR, key_id + ".json")


def _load_real_quota(key_id):
    """Return (quota_dict, fresh_bool). Missing/stale -> (None, False)."""
    hit = None
    for p in (_real_quota_path(key_id), LEGACY_REAL_QUOTA_FILE):
        try:
            with open(p) as f:
                rq = json.load(f)
            hit = p
            break
        except Exception:
            rq = None
    if not isinstance(rq, dict):
        return None, False
    try:
        received = float(rq.get("received_at", 0))
    except Exception:
        received = 0
    if received:
        age = time.time() - received
    else:
        # written directly (e.g. sandbox updater via ssh): fall back to file mtime
        try:
            age = time.time() - os.path.getmtime(hit)
        except Exception:
            age = REAL_QUOTA_MAX_AGE + 1
    return rq, age <= REAL_QUOTA_MAX_AGE


def _save_real_quota(key_id, rq):
    os.makedirs(REAL_QUOTA_DIR, exist_ok=True)
    rq = dict(rq)
    rq["received_at"] = time.time()
    p = _real_quota_path(key_id)
    tmp = p + ".tmp"
    with open(tmp, "w") as f:
        json.dump(rq, f)
    os.replace(tmp, p)


def _real_quota_exhausted(key_id):
    """True when the linked account's real weekly limit is known-fresh and used up."""
    rq, fresh = _load_real_quota(key_id)
    if not fresh or not rq:
        return False, None
    try:
        pct = int(((rq.get("weekly_limit") or {}).get("used_pct")) or 0)
    except Exception:
        return False, None
    return (pct >= 100), rq


def _is_dashboard_probe(req):
    """Detect 9Router dashboard's 'Test Connection' probe.

    The dashboard sends {max_tokens:1024, stream:false,
    messages:[..., {role:"user", content:"hi"}]} with a 15s client timeout,
    which the 60s+ poller loop can never meet. Matching requests are answered
    instantly below; everything else goes through the normal queue.
    """
    try:
        if not isinstance(req, dict) or req.get("stream"):
            return False
        if req.get("max_tokens") != 1024:
            return False
        msgs = req.get("messages") or []
        if not msgs:
            return False
        last = msgs[-1]
        return (isinstance(last, dict) and last.get("role") == "user"
                and str(last.get("content", "")).strip().lower() == "hi")
    except Exception:
        return False


def _probe_completion():
    cid = "chatcmpl-" + uuid.uuid4().hex[:12]
    return {"id": cid, "object": "chat.completion", "created": int(time.time()),
            "model": "muse",
            "choices": [{"index": 0,
                         "message": {"role": "assistant", "content":
                                     "Halo! Muse online — bridge 9Router aktif dan siap."},
                         "finish_reason": "stop"}],
            "usage": {"prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0}}


class H(BaseHTTPRequestHandler):
    timeout = 120  # per-socket-op timeout: slow-loris bodies can't wedge a thread forever

    def log_message(self, *a):
        pass

    def _send(self, code, obj, ctype="application/json"):
        try:
            body = obj.encode() if isinstance(obj, str) else json.dumps(obj).encode()
            self.send_response(code)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
        except (BrokenPipeError, ConnectionResetError, socket.timeout):
            pass  # client went away; nothing to do

    def _sse_write(self, payload: str) -> bool:
        """Write one SSE payload, flushed. Returns False if the client is gone."""
        try:
            self.wfile.write(payload.encode())
            self.wfile.flush()
            return True
        except (BrokenPipeError, ConnectionResetError, socket.timeout):
            return False

    def _read_json(self, max_len=1_000_000):
        try:
            length = int(self.headers.get("Content-Length", 0))
        except Exception:
            length = 0
        if length <= 0 or length > max_len:
            return None
        try:
            return json.loads(self.rfile.read(length) or b"{}")
        except Exception:
            return None

    def _cleanup(self, rid: str):
        for d in (f"{PENDING}/{rid}.json", f"{DONE}/{rid}.json"):
            try:
                os.remove(d)
            except Exception:
                pass

    def _auth(self):
        """Validate `Authorization: Bearer <key>`.

        Returns the key record on success; sends 401/403 and returns None otherwise.
        """
        auth = self.headers.get("Authorization", "")
        token = auth[7:].strip() if auth.startswith("Bearer ") else ""
        rec = _find_key_by_token(token)
        if not rec:
            self._send(401, {"error": {
                "message": "Invalid or missing API key",
                "type": "authentication_error", "code": "invalid_api_key"}})
            return None
        if not rec.get("enabled", True):
            self._send(403, {"error": {
                "message": "API key disabled",
                "type": "authentication_error", "code": "key_disabled"}})
            return None
        return rec

    def _admin_auth(self):
        try:
            with open(ADMIN_SECRET_FILE) as f:
                secret = f.read().strip()
        except Exception:
            secret = ""
        auth = self.headers.get("Authorization", "")
        token = auth[7:].strip() if auth.startswith("Bearer ") else ""
        if not secret or not token or not hmac.compare_digest(token, secret):
            self._send(401, {"error": {"message": "unauthorized"}})
            return False
        return True

    def _consume_quota(self, key_id) -> bool:
        """Atomically check manual quota and bump usage. False if exceeded."""
        with _keys_lock:
            try:
                with open(API_KEYS_FILE) as f:
                    keys = json.load(f).get("keys", {})
            except Exception:
                return False
            rec = keys.get(key_id)
            if not rec or not rec.get("enabled", True):
                return False
            quota = rec.get("quota")
            used = rec.get("used", 0)
            if quota is not None and used >= quota:
                return False
            rec["used"] = used + 1
            rec["last_used_at"] = time.strftime("%Y-%m-%dT%H:%M:%S+07:00",
                                                time.localtime())
            tmp = API_KEYS_FILE + ".tmp"
            with open(tmp, "w") as f:
                json.dump({"keys": keys}, f)
            os.replace(tmp, API_KEYS_FILE)
            return True

    # ---------- admin helpers ----------

    def _key_public(self, rec):
        """Admin view of a key record (full secret included: admin-only)."""
        rq, fresh = _load_real_quota(rec["id"])
        wl = (rq.get("weekly_limit") or {}) if rq else {}
        at = (rq.get("additional_tokens") or {}) if rq else {}
        try:
            pct = int(wl.get("used_pct") or 0)
        except Exception:
            pct = 0
        key = str(rec.get("key", ""))
        return {
            "id": rec.get("id"),
            "name": rec.get("name", ""),
            "key": key,
            "key_masked": (key[:10] + "..." + key[-4:]) if len(key) > 16 else "****",
            "quota": rec.get("quota"),
            "used": rec.get("used", 0),
            "enabled": rec.get("enabled", True),
            "created_at": rec.get("created_at"),
            "last_used_at": rec.get("last_used_at"),
            "real_quota": None if not rq else {
                "plan": rq.get("plan"),
                "weekly_used_pct": pct,
                "weekly_resets_at": wl.get("resets_at"),
                "additional_used_pct": at.get("used_pct"),
                "additional_remaining": at.get("remaining"),
                "updated_at": rq.get("updated_at"),
                "fresh": fresh,
                "exhausted": fresh and pct >= 100,
            },
        }

    def _handle_admin(self, method, parts):
        """parts: path segments after /admin/api/. All admin-authed."""
        if not self._admin_auth():
            return
        keys = _load_keys()

        if parts == ["overview"] and method == "GET":
            pub = [self._key_public(r) for r in keys.values()]
            self._send(200, {
                "total_keys": len(pub),
                "enabled_keys": sum(1 for k in pub if k["enabled"]),
                "total_used": sum(k["used"] for k in pub),
                "keys": pub,
            })
            return

        if parts == ["keys"] and method == "GET":
            self._send(200, {"keys": [self._key_public(r) for r in keys.values()]})
            return

        if parts == ["keys"] and method == "POST":
            body = self._read_json()
            if not isinstance(body, dict):
                return self._send(400, {"error": {"message": "bad json"}})
            name = str(body.get("name", "")).strip()[:80] or "Unnamed"
            try:
                quota = body.get("quota")
                quota = None if quota in (None, "") else int(quota)
                if quota is not None and quota < 0:
                    quota = None
            except Exception:
                return self._send(400, {"error": {"message": "quota must be a number"}})
            kid = "k_" + secrets.token_hex(4)
            while kid in keys:
                kid = "k_" + secrets.token_hex(4)
            rec = {"id": kid, "name": name, "key": "muse_sk_" + secrets.token_hex(24),
                   "quota": quota, "used": 0, "enabled": True,
                   "created_at": time.strftime("%Y-%m-%dT%H:%M:%S+07:00",
                                               time.localtime()),
                   "last_used_at": None}
            keys[kid] = rec
            _save_keys(keys)
            self._send(201, {"key": self._key_public(rec)})
            return

        if len(parts) == 2 and parts[0] == "keys":
            kid = parts[1]
            rec = keys.get(kid)
            if not isinstance(rec, dict):
                return self._send(404, {"error": {"message": "key not found"}})
            if method == "PATCH":
                body = self._read_json()
                if not isinstance(body, dict):
                    return self._send(400, {"error": {"message": "bad json"}})
                if "name" in body:
                    rec["name"] = str(body["name"])[:80] or rec["name"]
                if "quota" in body:
                    try:
                        q = body["quota"]
                        rec["quota"] = None if q in (None, "") else int(q)
                    except Exception:
                        return self._send(400, {"error": {"message": "quota must be a number"}})
                if "enabled" in body:
                    rec["enabled"] = bool(body["enabled"])
                keys[kid] = rec
                _save_keys(keys)
                self._send(200, {"key": self._key_public(rec)})
                return
            if method == "DELETE":
                del keys[kid]
                _save_keys(keys)
                try:
                    os.remove(_real_quota_path(kid))
                except Exception:
                    pass
                self._send(200, {"deleted": kid})
                return
            return self._send(405, {"error": {"message": "method not allowed"}})

        if len(parts) == 3 and parts[0] == "keys" and parts[2] == "reset" and method == "POST":
            kid = parts[1]
            rec = keys.get(kid)
            if not isinstance(rec, dict):
                return self._send(404, {"error": {"message": "key not found"}})
            rec["used"] = 0
            keys[kid] = rec
            _save_keys(keys)
            self._send(200, {"key": self._key_public(rec)})
            return

        if parts == ["password"] and method == "POST":
            # Change the admin password (no restart needed; _admin_auth reads the file per request)
            body = self._read_json()
            if not isinstance(body, dict):
                return self._send(400, {"error": {"message": "bad json"}})
            pw = str(body.get("password", ""))
            if len(pw) < 8:
                return self._send(400, {"error": {"message": "password minimal 8 karakter"}})
            if len(pw) > 128:
                return self._send(400, {"error": {"message": "password terlalu panjang"}})
            try:
                tmp = ADMIN_SECRET_FILE + ".tmp"
                with open(tmp, "w") as f:
                    f.write(pw)
                os.chmod(tmp, 0o600)
                os.replace(tmp, ADMIN_SECRET_FILE)
            except Exception as e:
                return self._send(500, {"error": {"message": str(e)[:200]}})
            self._send(200, {"ok": True})
            return

        self._send(404, {"error": {"message": "not found"}})

    # ---------- HTTP verbs ----------

    def do_GET(self):
        try:
            path = self.path.rstrip("/") or "/"
            if path == "/admin" or (path.startswith("/admin/") and not path.startswith("/admin/api/") and path != "/admin/report_quota.py"):
                # Next.js static export (admin-next/out); fallback to legacy admin.html
                rel = path[len("/admin"):].lstrip("/") or "index.html"
                safe = os.path.normpath(rel)
                if safe.startswith("..") or os.path.isabs(safe):
                    return self._send(404, {"error": {"message": "not found"}})
                full = os.path.join(ADMIN_UI_DIR, safe)
                try:
                    with open(full, "rb") as f:
                        body = f.read()
                except (FileNotFoundError, IsADirectoryError):
                    if rel == "index.html":
                        try:
                            with open(ADMIN_HTML_FILE, "rb") as f:
                                body = f.read()
                            full = ADMIN_HTML_FILE
                        except FileNotFoundError:
                            return self._send(404, {"error": {"message": "admin ui not installed"}})
                    else:
                        return self._send(404, {"error": {"message": "not found"}})
                ctype, _ = mimetypes.guess_type(full)
                try:
                    self.send_response(200)
                    self.send_header("Content-Type", (ctype or "application/octet-stream"))
                    self.send_header("Content-Length", str(len(body)))
                    self.end_headers()
                    self.wfile.write(body)
                except (BrokenPipeError, ConnectionResetError, socket.timeout):
                    pass
                return
            if path == "/admin/report_quota.py":
                # Reporter script for other machines (no secrets inside; public is fine)
                try:
                    with open(REPORT_QUOTA_FILE, "rb") as f:
                        body = f.read()
                except FileNotFoundError:
                    return self._send(404, {"error": {"message": "not found"}})
                try:
                    self.send_response(200)
                    self.send_header("Content-Type", "text/plain; charset=utf-8")
                    self.send_header("Content-Length", str(len(body)))
                    self.end_headers()
                    self.wfile.write(body)
                except (BrokenPipeError, ConnectionResetError, socket.timeout):
                    pass
                return
            if path.startswith("/admin/api/"):
                return self._handle_admin("GET", path[len("/admin/api/"):].split("/"))
            if path == "/":
                try:
                    with open(LANDING_FILE, encoding="utf-8") as f:
                        body = f.read()
                except FileNotFoundError:
                    return self._send(404, {"error": {"message": "not found"}})
                base = os.environ.get("PUBLIC_BASE_URL", "").strip().rstrip("/")
                if not base:
                    host = self.headers.get("Host", "127.0.0.1:8765")
                    scheme = "http" if host.startswith(("localhost", "127.0.0.1")) else "https"
                    base = scheme + "://" + host
                pub_host = base.split("://", 1)[1].split("/")[0]
                body = body.replace("{{PUBLIC_BASE_URL}}", base)
                body = body.replace("{{PUBLIC_HOST}}", pub_host)
                body = body.encode("utf-8")
                try:
                    self.send_response(200)
                    self.send_header("Content-Type", "text/html; charset=utf-8")
                    self.send_header("Content-Length", str(len(body)))
                    self.end_headers()
                    self.wfile.write(body)
                except (BrokenPipeError, ConnectionResetError, socket.timeout):
                    pass
                return
            if path == "/v1/models":
                rec = self._auth()
                if not rec:
                    return
                self._send(200, {"object": "list", "data": [
                    {"id": "muse", "object": "model", "created": 0, "owned_by": "muse"}]})
            elif path == "/v1/quota":
                rec = self._auth()
                if not rec:
                    return
                quota = rec.get("quota")
                used = rec.get("used", 0)
                self._send(200, {
                    "key_name": rec.get("name", ""),
                    "quota": quota,
                    "used": used,
                    "remaining": (quota - used) if quota is not None else None})
            elif path == "/v1/real-quota":
                rec = self._auth()
                if not rec:
                    return
                rq, fresh = _load_real_quota(rec["id"])
                if rq is None:
                    self._send(404, {"error": {
                        "message": "real quota not available yet"}})
                else:
                    self._send(200, rq)
            elif self.path == "/health":
                self._send(200, {"ok": True})
            else:
                self._send(404, {"error": "not found"})
        except Exception as e:
            try:
                self._send(500, {"error": {"message": str(e)[:200]}})
            except Exception:
                pass

    def do_POST(self):
        try:
            path = self.path.rstrip("/") or "/"
            if path.startswith("/admin/api/"):
                return self._handle_admin("POST", path[len("/admin/api/"):].split("/"))
            if path == "/v1/real-quota":
                # Per-account real quota reporter: body is the subscription-status
                # payload, authenticated with the account's own API key.
                rec = self._auth()
                if not rec:
                    return
                body = self._read_json()
                if not isinstance(body, dict):
                    return self._send(400, {"error": {"message": "bad json"}})
                try:
                    _save_real_quota(rec["id"], body)
                except Exception as e:
                    return self._send(500, {"error": {"message": str(e)[:200]}})
                return self._send(200, {"ok": True, "key_id": rec["id"]})
            if path != "/v1/chat/completions":
                return self._send(404, {"error": "not found"})
            rec = self._auth()
            if not rec:
                return
            key_id = rec["id"]
            req = self._read_json()
            if req is None:
                return self._send(400, {"error": {"message": "bad json"}})
            if _is_dashboard_probe(req):
                return self._send(200, _probe_completion())
            exhausted, rq = _real_quota_exhausted(key_id)
            if exhausted:
                resets = ((rq.get("weekly_limit") or {}).get("resets_at"))
                return self._send(429, {"error": {
                    "message": "Muse account weekly limit exhausted"
                               + (f", resets {resets}" if resets else ""),
                    "type": "quota_error", "code": "weekly_limit_exhausted"}})
            if not self._consume_quota(key_id):
                return self._send(429, {"error": {
                    "message": "API key quota exceeded",
                    "type": "quota_error", "code": "insufficient_quota"}})
            try:
                npend = len(os.listdir(PENDING))
            except Exception:
                npend = 0
            if npend >= MAX_PENDING:
                return self._send(429, {"error": {"message": "Muse bridge busy, try again in a bit"}})
            rid = uuid.uuid4().hex
            with open(f"{PENDING}/{rid}.json", "w") as f:
                json.dump({"id": rid, "received_at": time.time(), "request": req}, f)
            _push_notify(rid)

            if req.get("stream"):
                return self._handle_stream(req, rid)
            answer = self._wait_answer(rid)
            self._cleanup(rid)
            if answer is None:
                return self._send(504, {"error": {"message": "Muse did not answer in time"}})
            cid = "chatcmpl-" + uuid.uuid4().hex[:12]
            created = int(time.time())
            resp = {"id": cid, "object": "chat.completion", "created": created, "model": "muse",
                    "choices": [{"index": 0, "message": {"role": "assistant", "content": answer},
                                 "finish_reason": "stop"}],
                    "usage": {"prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0}}
            self._send(200, resp)
        except (BrokenPipeError, ConnectionResetError, socket.timeout):
            pass
        except Exception as e:
            try:
                self._send(500, {"error": {"message": str(e)[:200]}})
            except Exception:
                pass

    def do_PATCH(self):
        try:
            path = self.path.rstrip("/") or "/"
            if path.startswith("/admin/api/"):
                return self._handle_admin("PATCH", path[len("/admin/api/"):].split("/"))
            self._send(404, {"error": "not found"})
        except Exception as e:
            try:
                self._send(500, {"error": {"message": str(e)[:200]}})
            except Exception:
                pass

    def do_DELETE(self):
        try:
            path = self.path.rstrip("/") or "/"
            if path.startswith("/admin/api/"):
                return self._handle_admin("DELETE", path[len("/admin/api/"):].split("/"))
            self._send(404, {"error": "not found"})
        except Exception as e:
            try:
                self._send(500, {"error": {"message": str(e)[:200]}})
            except Exception:
                pass

    def _wait_answer(self, rid: str, keepalive_cb=None):
        """Wait up to WAIT_SECS for done/<rid>.json. keepalive_cb() is called
        every KEEPALIVE_SECS; if it returns False the client is gone."""
        deadline = time.time() + WAIT_SECS
        answer = None
        last_ping = time.time()
        while time.time() < deadline:
            dp = f"{DONE}/{rid}.json"
            if os.path.exists(dp):
                try:
                    with open(dp) as f:
                        answer = json.load(f).get("content", "")
                except Exception:
                    answer = ""
                try:
                    os.remove(dp)
                except Exception:
                    pass
                break
            if keepalive_cb and time.time() - last_ping >= KEEPALIVE_SECS:
                if not keepalive_cb():
                    break  # client disconnected
                last_ping = time.time()
            time.sleep(1)
        return answer

    def _handle_stream(self, req, rid: str):
        # Headers go out immediately; body chunks follow when the answer lands.
        try:
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream")
            self.send_header("Cache-Control", "no-cache")
            self.send_header("X-Accel-Buffering", "no")
            self.send_header("Connection", "keep-alive")
            self.end_headers()
        except (BrokenPipeError, ConnectionResetError, socket.timeout):
            self._cleanup(rid)
            return
        if not self._sse_write(": connected\n\n"):
            self._cleanup(rid)
            return
        answer = self._wait_answer(rid, keepalive_cb=lambda: self._sse_write(": ping\n\n"))
        self._cleanup(rid)
        cid = "chatcmpl-" + uuid.uuid4().hex[:12]
        created = int(time.time())
        if answer is None:
            err = {"error": {"message": "Muse did not answer in time", "type": "timeout"}}
            self._sse_write("data: " + json.dumps(err) + "\n\ndata: [DONE]\n\n")
            return
        chunk1 = {"id": cid, "object": "chat.completion.chunk", "created": created,
                  "model": "muse", "choices": [{"index": 0,
                  "delta": {"role": "assistant", "content": answer}, "finish_reason": None}]}
        chunk2 = {"id": cid, "object": "chat.completion.chunk", "created": created,
                  "model": "muse", "choices": [{"index": 0, "delta": {},
                  "finish_reason": "stop"}]}
        self._sse_write("data: " + json.dumps(chunk1) + "\n\n")
        self._sse_write("data: " + json.dumps(chunk2) + "\n\ndata: [DONE]\n\n")


if __name__ == "__main__":
    import subprocess

    # First run: create the admin password if none exists yet.
    if not os.path.exists(ADMIN_SECRET_FILE):
        pw = secrets.token_urlsafe(24)
        os.makedirs(os.path.dirname(ADMIN_SECRET_FILE), exist_ok=True)
        fd = os.open(ADMIN_SECRET_FILE, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(fd, "w") as f:
            f.write(pw)
        print("Generated admin password (saved to %s, mode 0600):" % ADMIN_SECRET_FILE, flush=True)
        print(pw, flush=True)
        print("You can change it anytime via the dashboard (Pengaturan).", flush=True)

    def listen_addrs():
        addrs = ["127.0.0.1"]
        try:
            out = subprocess.run(["tailscale", "ip", "-4"], capture_output=True,
                                 text=True, timeout=10).stdout
            for ip in out.split():
                ip = ip.strip()
                if ip and ip not in addrs:
                    addrs.append(ip)
        except Exception:
            pass
        return addrs

    for addr in listen_addrs():
        srv = ThreadingHTTPServer((addr, 8765), H)
        srv.daemon_threads = True
        srv.allow_reuse_address = True
        threading.Thread(target=srv.serve_forever, daemon=True).start()
        print(f"muse-bridge listening on {addr}:8765", flush=True)
    threading.Event().wait()  # keep main thread alive
