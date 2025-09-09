#!/usr/bin/env python3
from flask import Flask, request, jsonify, abort
import os, requests, json, urllib.request

# Auth shared with the Mac inbound bridge
VM_SHARED_SECRET = os.getenv("VM_SHARED_SECRET", "super-secret")

# Where to send replies on the Mac
MAC_RELAY_URL = os.getenv("MAC_RELAY_URL", "http://YOUR-MAC-IP:5055/send_imessage")
MAC_RELAY_SECRET = os.getenv("MAC_RELAY_SECRET", "super-secret")

# OpenAI
OPENAI_API_KEY = os.getenv("OPENAI_API_KEY", "")

# VIPs who should NOT get auto replies
VIP_SKIP = set([s.strip() for s in os.getenv("VIP_SKIP", "").split(",") if s.strip()])

app = Flask(__name__)

def gpt_reply(user_text, phone):
    req = urllib.request.Request(
        "https://api.openai.com/v1/chat/completions",
        headers={
            "Authorization": f"Bearer {OPENAI_API_KEY}",
            "Content-Type": "application/json",
        },
        data=json.dumps({
            "model": "gpt-5-mini",
            "messages": [
                {"role":"system","content":"You are a helpful fundraising assistant for the Martin HS Band."},
                {"role":"user","content": f"From {phone}: {user_text}\nReply concisely and kindly. If they ask to donate or for link, include https://bit.ly/AndreeBand"}
            ],
            "temperature": 0.4
        }).encode("utf-8")
    )
    with urllib.request.urlopen(req, timeout=20) as resp:
        data = json.loads(resp.read().decode("utf-8"))
    return data["choices"][0]["message"]["content"].strip()

def send_via_mac(phone, text):
    r = requests.post(
        MAC_RELAY_URL,
        headers={"X-Relay-Secret": MAC_RELAY_SECRET, "Content-Type": "application/json"},
        json={"to": phone, "text": text},
        timeout=15
    )
    r.raise_for_status()
    return r.json()

@app.post("/imessage/incoming")
def imessage_incoming():
    # simple auth from the Mac inbound bridge
    if request.headers.get("X-Shared-Secret") != VM_SHARED_SECRET:
        abort(401)
    data = request.get_json(force=True, silent=True) or {}
    phone = (data.get("from") or "").strip()
    text  = (data.get("text") or "").strip()

    if not phone or not text:
        return jsonify({"ignored": True}), 200

    # Skip VIPs
    if phone in VIP_SKIP:
        return jsonify({"skipped":"VIP"}), 200

    try:
        reply = gpt_reply(text, phone)
        send_via_mac(phone, reply)
        return jsonify({"ok": True})
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)}), 500

@app.get("/health")
def health():
    return jsonify({"ok": True})

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.getenv("PORT","5070")))

