#!/usr/bin/env python3
"""Contoh worker polling untuk Muse Bridge.

Bridge mengantrekan request chat (format OpenAI) sebagai file
queue/pending/<id>.json dan menunggu jawaban di queue/done/<id>.json
(maksimal 240 detik). Worker ini:

  1. List queue/pending/*.json
  2. Klaim satu request secara atomik: rename pending/ -> processing/
     (supaya dua worker tidak menjawab request yang sama)
  3. Baca {"id", "received_at", "request"} — "request" adalah body chat OpenAI.
  4. Jawab via fungsi answer() — GANTI dengan pemanggilan agent/AI kamu.
  5. Tulis {"content": jawaban} ke queue/done/<id>.json
  6. Hapus file processing/

Jalankan tiap 10-60 detik via cron, atau loop:
    while true; do python3 worker_example.py; sleep 15; done

Request berumur > 220 detik dilewati (bridge sudah menyerah menunggu).
"""

import json
import os
import sys
import time

BASE = os.environ.get("MUSE_BRIDGE_DIR", "/home/ubuntu/muse-bridge")
PENDING = os.path.join(BASE, "queue", "pending")
PROCESSING = os.path.join(BASE, "queue", "processing")
DONE = os.path.join(BASE, "queue", "done")
MAX_AGE = 220  # detik; lebih tua dari ini -> bridge sudah timeout


def answer(openai_request):
    """GANTI fungsi ini dengan pemanggilan agent/AI kamu.

    Menerima body request OpenAI (/v1/chat/completions), harus mengembalikan
    string jawaban asisten.
    """
    msgs = openai_request.get("messages", [])
    last = msgs[-1].get("content", "") if msgs else ""
    return ("[worker_example] Ganti fungsi answer() dengan agent AI kamu. "
            "Pesan terakhir user: %s" % last)


def main():
    os.makedirs(PROCESSING, exist_ok=True)
    try:
        names = sorted(os.listdir(PENDING))
    except FileNotFoundError:
        return 0
    for name in names:
        if not name.endswith(".json"):
            continue
        src = os.path.join(PENDING, name)
        dst = os.path.join(PROCESSING, name)
        try:
            os.rename(src, dst)  # klaim atomik
        except FileNotFoundError:
            continue  # worker lain sudah mengambil
        try:
            with open(dst) as f:
                payload = json.load(f)
        except Exception:
            os.remove(dst)
            continue
        rid = payload.get("id") or name[:-5]
        if time.time() - payload.get("received_at", 0) > MAX_AGE:
            os.remove(dst)  # bridge sudah timeout; jangan dijawab
            continue
        try:
            content = answer(payload.get("request", {}))
        except Exception as e:
            content = "[worker error: %s]" % e
        with open(os.path.join(DONE, rid + ".json"), "w") as f:
            json.dump({"content": content}, f)
        os.remove(dst)
    return 0


if __name__ == "__main__":
    sys.exit(main())
