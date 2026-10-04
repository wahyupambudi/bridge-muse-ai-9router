#!/usr/bin/env python3
"""
muse-bridge — expose the Muse agent ke 9router sebagai provider OpenAI-compatible.

Arsitektur (dipilih karena sandbox VPS tidak bisa menerima koneksi inbound):
  - bridge jalan di Railway (publik), satu project dengan 9router.
  - 9router -> POST {BRIDGE_URL}/v1/chat/completions  (format OpenAI)
  - bridge -> antrekan request, TAHAN koneksi (long-poll s/d BRIDGE_TIMEOUT detik)
  - agen (cron poller, outbound dari sandbox) -> GET /internal/queue
  - agen -> POST /internal/respond {"id": ..., "reply": "..."}
  - bridge -> balas ke 9router dalam format OpenAI chat.completion

Env:
  PORT            port listen (Railway mengisi otomatis)
  BRIDGE_API_KEY  Bearer token untuk 9router -> bridge (wajib di production)
  AGENT_KEY       Bearer token untuk agen -> bridge (default = BRIDGE_API_KEY)
  BRIDGE_TIMEOUT  detik long-poll maksimum (default 100)
  BRIDGE_MODEL    id model yang diiklankan ke 9router (default "koda/muse")

Stdlib only — tanpa dependencies.
"""

import json
import os
import threading
import time
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

PORT = int(os.environ.get("PORT", "8765"))
BRIDGE_API_KEY = os.environ.get("BRIDGE_API_KEY", "")
AGENT_KEY = os.environ.get("AGENT_KEY", "") or BRIDGE_API_KEY
TIMEOUT = int(os.environ.get("BRIDGE_TIMEOUT", "100"))
MODEL_ID = os.environ.get("BRIDGE_MODEL", "koda/muse")

jobs = {}  # id -> {"request":..., "event":Event, "response":str|None, "created":ts}
jobs_lock = threading.Lock()


def _send(handler, code, obj, content_type="application/json"):
    body = obj.encode() if isinstance(obj, str) else json.dumps(obj).encode()
    handler.send_response(code)
    handler.send_header("Content-Type", content_type)
    handler.send_header("Content-Length", str(len(body)))
    handler.end_headers()
    handler.wfile.write(body)


def _unauthorized(handler):
    _send(handler, 401, {"error": {"message": "invalid or missing API key",
                                   "type": "auth", "code": 401}})


def _check(handler, key):
    if not key:
        return True
    return handler.headers.get("Authorization", "") == f"Bearer {key}"


def _read_json(handler):
    length = int(handler.headers.get("Content-Length", 0) or 0)
    if not length:
        return {}
    try:
        return json.loads(handler.rfile.read(length).decode("utf-8"))
    except Exception:
        return {}


def openai_response(model, reply):
    return {
        "id": f"chatcmpl-{uuid.uuid4().hex[:12]}",
        "object": "chat.completion",
        "created": int(time.time()),
        "model": model,
        "choices": [{
            "index": 0,
            "message": {"role": "assistant", "content": reply},
            "finish_reason": "stop",
        }],
        "usage": {"prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0},
    }


class Handler(BaseHTTPRequestHandler):
    server_version = "muse-bridge/1.0"

    def log_message(self, fmt, *args):
        print(f"[{time.strftime('%H:%M:%S')}] {self.address_string()} {fmt % args}",
              flush=True)

    def do_GET(self):
        path = self.path.split("?")[0]
        if path == "/healthz":
            _send(self, 200, {"ok": True})
        elif path == "/v1/models":
            if not _check(self, BRIDGE_API_KEY):
                return _unauthorized(self)
            _send(self, 200, {"object": "list", "data": [
                {"id": MODEL_ID, "object": "model",
                 "created": int(time.time()), "owned_by": "koda"}
            ]})
        elif path == "/internal/queue":
            if not _check(self, AGENT_KEY):
                return _unauthorized(self)
            with jobs_lock:
                items = [{"id": jid,
                          "model": j["request"].get("model", MODEL_ID),
                          "messages": j["request"].get("messages", []),
                          "created": j["created"]}
                         for jid, j in jobs.items() if j["response"] is None]
            _send(self, 200, {"jobs": items})
        else:
            _send(self, 404, {"error": {"message": "not found", "code": 404}})

    def do_POST(self):
        path = self.path.split("?")[0]
        if path == "/v1/chat/completions":
            if not _check(self, BRIDGE_API_KEY):
                return _unauthorized(self)
            body = _read_json(self)
            messages = body.get("messages", [])
            if not messages:
                _send(self, 400, {"error": {"message": "messages kosong",
                                            "type": "invalid_request", "code": 400}})
                return
            jid = uuid.uuid4().hex[:12]
            job = {"request": body, "event": threading.Event(),
                   "response": None, "created": time.time()}
            with jobs_lock:
                jobs[jid] = job
            self.log_message("job %s queued (%d messages)", jid, len(messages))
            answered = job["event"].wait(TIMEOUT)
            with jobs_lock:
                job = jobs.pop(jid, None)
            if not answered or not job or job["response"] is None:
                _send(self, 504, {"error": {
                    "message": f"agent timeout: tidak ada jawaban dalam {TIMEOUT} detik",
                    "type": "timeout", "code": 504}})
                return
            reply = job["response"]
            model = body.get("model", MODEL_ID)
            if body.get("stream"):
                self._send_stream(model, reply)
            else:
                _send(self, 200, openai_response(model, reply))
        elif path == "/internal/respond":
            if not _check(self, AGENT_KEY):
                return _unauthorized(self)
            body = _read_json(self)
            jid, reply = body.get("id"), body.get("reply", "")
            with jobs_lock:
                job = jobs.get(jid)
                if job and job["response"] is None:
                    job["response"] = reply
                    job["event"].set()
                    _send(self, 200, {"ok": True, "id": jid})
                else:
                    _send(self, 404, {"error": {
                        "message": "job tidak ditemukan / sudah dijawab",
                        "code": 404}})
        else:
            _send(self, 404, {"error": {"message": "not found", "code": 404}})

    def _send_stream(self, model, reply):
        # SSE minimal: satu chunk penuh + [DONE]
        cid = f"chatcmpl-{uuid.uuid4().hex[:12]}"
        created = int(time.time())
        chunk = {"id": cid, "object": "chat.completion.chunk", "created": created,
                 "model": model, "choices": [
                     {"index": 0, "delta": {"role": "assistant", "content": reply},
                      "finish_reason": None}]}
        done = {"id": cid, "object": "chat.completion.chunk", "created": created,
                "model": model, "choices": [
                    {"index": 0, "delta": {}, "finish_reason": "stop"}]}
        payload = "".join(f"data: {json.dumps(p)}\n\n" for p in (chunk, done))
        payload += "data: [DONE]\n\n"
        body = payload.encode()
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.send_header("Cache-Control", "no-cache")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


def reaper():
    """Buang job basi supaya koneksi tidak gantung selamanya."""
    while True:
        time.sleep(30)
        cutoff = time.time() - (TIMEOUT + 60)
        with jobs_lock:
            stale = [jid for jid, j in jobs.items()
                     if j["created"] < cutoff and j["response"] is None]
            for jid in stale:
                jobs[jid]["event"].set()
                del jobs[jid]
        if stale:
            print(f"[reaper] membuang {len(stale)} job basi", flush=True)


if __name__ == "__main__":
    threading.Thread(target=reaper, daemon=True).start()
    srv = ThreadingHTTPServer(("0.0.0.0", PORT), Handler)
    print(f"muse-bridge listen di 0.0.0.0:{PORT} model={MODEL_ID} "
          f"timeout={TIMEOUT}s", flush=True)
    if not BRIDGE_API_KEY:
        print("PERINGATAN: BRIDGE_API_KEY kosong — API terbuka tanpa auth!",
              flush=True)
    srv.serve_forever()
