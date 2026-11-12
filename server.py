"""Optimus: Noptimus (phone notifications on the laptop) and Moptimus (texts from the laptop
through the phone) in one server.

Noptimus
  phone --POST /notification--> Pi --/stream--> page

Moptimus
  laptop --/send--> Pi --ntfy push (ID only)--> phone
  phone --/claim/<id>--> Pi (gets name + text, once, within EXPIRY_SECONDS)
  phone: Find Contacts by name -> exactly one match? -> Send Message
  phone --/claim/<id>/sent or /failed--> Pi
"""
from flask import Flask, request, jsonify, Response, send_from_directory, abort
from collections import deque
from datetime import datetime
import ipaddress
import itertools
import json
import os
import queue
import secrets
import threading
import time
import urllib.request

PORT = 5000
RETENTION_HOURS = 8        # notifications and sent messages both stay this long
EXPIRY_SECONDS = 120       # phone must pick a message up within this, or it's dropped
MAX_MESSAGE_LENGTH = 2000

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
with open(os.path.join(BASE_DIR, "moptimus_config.json")) as f:
    CONFIG = json.load(f)
NTFY_URL = f"{CONFIG.get('ntfy_server', 'https://ntfy.sh').rstrip('/')}/{CONFIG['ntfy_topic']}"
# Only these contact names can be texted. Empty list = anyone (not recommended).
ALLOWED = [name.strip() for name in CONFIG.get("allowed_recipients", []) if name.strip()]


TAILNET_RANGES = [ipaddress.ip_network("100.64.0.0/10"), ipaddress.ip_network("fd7a:115c:a1e0::/48")]

app = Flask(__name__)


@app.route("/")
def page():
    return send_from_directory(BASE_DIR, "index.html")


# ---------------------------------------------------------------- Noptimus

notifications = deque()          # oldest on the left, newest on the right
next_id = itertools.count(1)
data_lock = threading.Lock()

subscribers = []                 # one queue per open page / client
subscribers_lock = threading.Lock()


def prune():
    """Drop anything older than RETENTION_HOURS. Call while holding data_lock."""
    cutoff = time.time() - RETENTION_HOURS * 3600
    while notifications and notifications[0]["received_at"] < cutoff:
        notifications.popleft()


@app.route("/notification", methods=["POST"])
def receive_notification():
    data = request.get_json(silent=True) or {}
    app_name = data.get("app")
    if app_name == "Business Suite":
        app_name = "Instagram"

    item = {
        "id": next(next_id),
        "received_at": time.time(),
        "date": data.get("date"),
        "app": app_name,
        "title": data.get("title"),
        "subtitle": data.get("subtitle"),
        "body": data.get("body"),
    }

    with data_lock:
        notifications.append(item)
        prune()
        stored = len(notifications)

    print(f"\n[{datetime.now():%H:%M:%S}] #{item['id']} {item['app']}: {item['title']}")
    if item["subtitle"]:
        print("  ", item["subtitle"])
    print("  ", item["body"])

    message = json.dumps(item)
    with subscribers_lock:
        for q in subscribers:
            q.put(message)

    return jsonify({"status": "received", "id": item["id"], "stored": stored}), 200


@app.route("/notifications")
def list_notifications():
    """Everything from the last RETENTION_HOURS, or only newer than ?since=<id>."""
    since = request.args.get("since", default=0, type=int)
    with data_lock:
        prune()
        items = [n for n in notifications if n["id"] > since]
    return jsonify(items)


@app.route("/stream")
def stream():
    q = queue.Queue()
    with subscribers_lock:
        subscribers.append(q)
        count = len(subscribers)
    print(f"Client connected ({count} open)")

    def event_stream():
        try:
            yield ": connected\n\n"
            while True:
                try:
                    yield f"data: {q.get(timeout=15)}\n\n"
                except queue.Empty:
                    yield ": keepalive\n\n"
        finally:
            with subscribers_lock:
                subscribers.remove(q)
                count = len(subscribers)
            print(f"Client disconnected ({count} open)")

    return Response(
        event_stream(),
        mimetype="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


# ---------------------------------------------------------------- Moptimus

outbox = {}                # id -> message, in creation order
outbox_lock = threading.Lock()


def tailnet_only():
    """Every Moptimus route can lead to a text being sent as you, so only your own devices get in."""
    if request.headers.get("Tailscale-Funnel-Request"):
        abort(403)
    addr = ipaddress.ip_address(request.remote_addr)
    if not (addr.is_loopback or any(addr in net for net in TAILNET_RANGES)):
        abort(403)


def tidy():
    """Expire messages the phone never picked up, and forget old ones. Hold outbox_lock."""
    now = time.time()
    for m in outbox.values():
        if m["status"] == "waiting" and now - m["created_at"] > EXPIRY_SECONDS:
            m["status"] = "expired"
    cutoff = now - RETENTION_HOURS * 3600
    for msg_id in [i for i, m in outbox.items() if m["created_at"] < cutoff]:
        del outbox[msg_id]


CONFIG_PATH = os.path.join(BASE_DIR, "moptimus_config.json")
_allowed_cache = {"mtime": None, "names": ALLOWED}


def current_allowed():
    """Re-reads the allowed list whenever the config file changes, so no restart is needed.
    If the file has a typo, keeps using the last good list."""
    try:
        mtime = os.path.getmtime(CONFIG_PATH)
        if mtime != _allowed_cache["mtime"]:
            with open(CONFIG_PATH) as f:
                names = json.load(f).get("allowed_recipients", [])
            _allowed_cache["names"] = [n.strip() for n in names if n.strip()]
            _allowed_cache["mtime"] = mtime
    except (OSError, ValueError) as e:
        print(f"[moptimus] couldn't reload config, keeping the old list: {e}")
    return _allowed_cache["names"]


def allowed_name(to):
    """Returns the exact allowed spelling, or None if the name isn't on the list."""
    names = current_allowed()
    if not names:
        return to
    for name in names:
        if name.lower() == to.lower():
            return name
    return None

def view(m):
    return {k: m[k] for k in ("id", "to", "text", "status", "reason", "created_at", "updated_at")}


@app.route("/recipients")
def recipients():
    tailnet_only()
    return jsonify(current_allowed())


@app.route("/send", methods=["POST"])
def send():
    tailnet_only()
    data = request.get_json(silent=True) or {}
    to = (data.get("to") or "").strip()
    text = (data.get("text") or "").strip()
    if not to or not text:
        return jsonify({"error": "add a recipient and a message"}), 400
    if len(text) > MAX_MESSAGE_LENGTH:
        return jsonify({"error": f"messages are capped at {MAX_MESSAGE_LENGTH} characters"}), 400
    name = allowed_name(to)
    if name is None:
        return jsonify({"error": f"{to} isn't on your allowed list"}), 400

    now = time.time()
    msg_id = secrets.token_urlsafe(6)
    item = {"id": msg_id, "to": name, "text": text, "status": "waiting",
            "reason": None, "created_at": now, "updated_at": now}
    with outbox_lock:
        tidy()
        outbox[msg_id] = item

    # The push carries only the ID. Knowing the topic can't make your phone text anyone.
    try:
        req = urllib.request.Request(NTFY_URL, data=msg_id.encode(), method="POST",
                                     headers={"Title": "Moptimus"})
        urllib.request.urlopen(req, timeout=10).read()
    except Exception as e:
        with outbox_lock:
            item.update(status="failed", reason="couldn't reach ntfy", updated_at=time.time())
        print(f"[moptimus] ntfy push failed for {msg_id}: {e}")
        return jsonify({"error": "couldn't reach ntfy, so your phone wasn't told", "item": view(item)}), 502

    print(f"[moptimus] {msg_id} queued for {name}")
    return jsonify(view(item)), 200


@app.route("/outbox")
def list_outbox():
    tailnet_only()
    with outbox_lock:
        tidy()
        return jsonify([view(m) for m in reversed(list(outbox.values()))])


@app.route("/claim/<msg_id>")
def claim(msg_id):
    """The phone picks a message up here. Works once, and only before it expires."""
    tailnet_only()
    with outbox_lock:
        tidy()
        item = outbox.get(msg_id)
        if not item or item["status"] != "waiting":
            return jsonify({}), 404
        item.update(status="on_phone", updated_at=time.time())
        print(f"[moptimus] {msg_id} picked up by phone")
        return jsonify({"to": item["to"], "text": item["text"]})


@app.route("/claim/<msg_id>/sent", methods=["POST"])
def mark_sent(msg_id):
    tailnet_only()
    with outbox_lock:
        item = outbox.get(msg_id)
        if not item or item["status"] != "on_phone":
            return jsonify({}), 404
        item.update(status="sent", updated_at=time.time())
    print(f"[moptimus] {msg_id} sent")
    return jsonify({"status": "sent"})


@app.route("/claim/<msg_id>/failed", methods=["POST"])
def mark_failed(msg_id):
    tailnet_only()
    data = request.get_json(silent=True) or {}
    reason = str(data.get("reason") or "the phone didn't send it")[:200]
    with outbox_lock:
        item = outbox.get(msg_id)
        if not item or item["status"] != "on_phone":
            return jsonify({}), 404
        item.update(status="failed", reason=reason, updated_at=time.time())
    print(f"[moptimus] {msg_id} failed: {reason}")
    return jsonify({"status": "failed"})


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=5000, threaded=True)
