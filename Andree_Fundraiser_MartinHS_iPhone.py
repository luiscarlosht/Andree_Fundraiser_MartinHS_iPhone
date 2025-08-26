#!/usr/bin/env python3
# -*- coding: utf-8 -*-

"""
Andree_Fundraiser_MartinHS_iPhone.py
------------------------------------
Stand-alone iMessage/SMS helper via BlueBubbles + GPT-5 auto-replies.

Features
- Flask webhook: /imessage/webhook (BlueBubbles -> VM)
- Health check:  /health
- Auto-reply with FAQ shortcuts, else GPT-5 (primary/fallback models)
- VIP exclusions (numbers you’ll reply to manually)
- Duplicate/rate limiting guards
- CSV logs for inbound/outbound
- CLI: runserver | send-one | bulk-send

Env Vars (required/optional)
- BLUEBUBBLES_URL          (required) e.g. http://192.168.1.10:12345
- BLUEBUBBLES_API_KEY      (required) BlueBubbles API key
- OPENAI_API_KEY           (required)
- OPENAI_PRIMARY_MODEL     (opt, default "gpt-5")
- OPENAI_FALLBACK_MODEL    (opt, default "gpt-5-mini")
- FUNDRAISER_URL           (opt, default "https://bit.ly/AndreeBand")
- GOAL_USD                 (opt, default "1000")
- DEADLINE                 (opt, ISO date "YYYY-MM-DD", default "")
- IMESSAGE_VIP_EXCLUDE     (opt) "+12145550123,+18175550123"
- RATE_WINDOW_SEC          (opt, default 5)
- DUP_WINDOW_SEC           (opt, default 10)
- IMESSAGE_SENT_LOG        (opt, default "imessage_sent.csv")
- IMESSAGE_INBOUND_LOG     (opt, default "imessage_inbound.csv")
- DELAY_SECONDS            (opt, default 0.6) for bulk-send pacing

CSV expectations for bulk-send
- Columns: Phone_E164 or Phone (E.164), GreetingName/FirstName/Name (optional)
- Optional: Language or language ("es"/"en"), Country ("MX" => ES)
"""

import os
import re
import csv
import time
import json
import logging
import argparse
import datetime
from collections import defaultdict

import requests
from flask import Flask, request

# -------------------------- Logging --------------------------
logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")

# -------------------------- Config / Env --------------------------
FUNDRAISER_URL = os.getenv("FUNDRAISER_URL", "https://bit.ly/AndreeBand")
GOAL_USD = os.getenv("GOAL_USD", "1000")
DEADLINE = os.getenv("DEADLINE", "")              # e.g., "2025-09-30"

BLUEBUBBLES_URL = os.getenv("BLUEBUBBLES_URL", "")
BLUEBUBBLES_API_KEY = os.getenv("BLUEBUBBLES_API_KEY", "")

PRIMARY_MODEL = os.getenv("OPENAI_PRIMARY_MODEL", "gpt-5")
FALLBACK_MODEL = os.getenv("OPENAI_FALLBACK_MODEL", "gpt-5-mini")
OPENAI_API_KEY = os.getenv("OPENAI_API_KEY")

if not BLUEBUBBLES_URL or not BLUEBUBBLES_API_KEY:
    logging.warning("Missing BLUEBUBBLES_URL / BLUEBUBBLES_API_KEY (required for send).")
if not OPENAI_API_KEY:
    logging.warning("Missing OPENAI_API_KEY (required for GPT replies).")

IMESSAGE_SENT_LOG = os.getenv("IMESSAGE_SENT_LOG", "imessage_sent.csv")
IMESSAGE_INBOUND_LOG = os.getenv("IMESSAGE_INBOUND_LOG", "imessage_inbound.csv")

RATE_WINDOW_SEC = int(os.getenv("RATE_WINDOW_SEC", "5"))
DUP_WINDOW_SEC  = int(os.getenv("DUP_WINDOW_SEC", "10"))
DELAY_SECONDS   = float(os.getenv("DELAY_SECONDS", "0.6"))

# Comma-separated E164 numbers to NOT auto-reply
IMESSAGE_VIP_EXCLUDE = set(
    x.strip() for x in os.getenv("IMESSAGE_VIP_EXCLUDE", "").split(",") if x.strip()
)

# -------------------------- Helpers: anti-spam --------------------------
_last_seen_ts = defaultdict(float)        # phone -> last timestamp
_last_seen_msg = {}                       # (phone, text) -> timestamp

def too_soon(phone: str) -> bool:
    now = time.time()
    if now - _last_seen_ts[phone] < RATE_WINDOW_SEC:
        return True
    _last_seen_ts[phone] = now
    return False

def is_duplicate(phone: str, text: str) -> bool:
    key = (phone, (text or "").strip().lower())
    now = time.time()
    ts = _last_seen_msg.get(key, 0.0)
    if now - ts < DUP_WINDOW_SEC:
        return True
    _last_seen_msg[key] = now
    return False

def fmt_deadline():
    if not DEADLINE:
        return ""
    try:
        d = datetime.datetime.fromisoformat(DEADLINE)
        return d.strftime("%B %d, %Y")
    except Exception:
        return DEADLINE

DEADLINE_HUMAN = fmt_deadline()

# -------------------------- Language detection --------------------------
def is_spanish(text: str) -> bool:
    t = (text or "").lower()
    spanish_markers = [
        "¿", "¡", "qué", "como", "cómo", "cuánto", "cuanto", "dónde", "para qué", "para que",
        "tarjeta", "mexicana", "donar", "donación", "pago", "ayudar", "compartir", "deducible"
    ]
    return any(w in t for w in spanish_markers)

def detect_lang_row(row) -> str:
    raw = (row.get("Language") or row.get("language") or "").strip().lower()
    if raw in ("es","es-mx","spanish","español"): return "ES"
    if raw in ("en","en-us","english"): return "EN"
    return "ES" if (row.get("Country","").strip().upper() == "MX") else "EN"

def first_name(row):
    return (row.get("GreetingName") or row.get("FirstName") or row.get("Name") or "friend").strip()

# -------------------------- OpenAI client --------------------------
from openai import OpenAI
from openai import APIError, RateLimitError, APITimeoutError
client = OpenAI(api_key=OPENAI_API_KEY) if OPENAI_API_KEY else None

def ask_openai(prompt: str, max_retries: int = 2) -> str:
    if not client:
        raise RuntimeError("OPENAI_API_KEY not set")
    models_to_try = [PRIMARY_MODEL, FALLBACK_MODEL]
    last_err = None
    for model in models_to_try:
        for attempt in range(1, max_retries + 1):
            try:
                logging.info(f"[OpenAI] model={model} attempt={attempt}")
                resp = client.responses.create(model=model, input=prompt)
                text = (resp.output_text or "").strip()
                if not text:
                    raise APIError("Empty response text")
                if model != PRIMARY_MODEL:
                    logging.warning(f"[OpenAI] FELL BACK to {model} and succeeded.")
                return text
            except (RateLimitError, APITimeoutError) as e:
                backoff = min(8.0, 1.5 ** attempt)
                logging.warning(f"[OpenAI] transient error: {e}; sleep {backoff:.1f}s")
                time.sleep(backoff)
                last_err = e
                continue
            except APIError as e:
                logging.error(f"[OpenAI] API error: {e}")
                last_err = e
                break
            except Exception as e:
                logging.exception(f"[OpenAI] unexpected: {e}")
                last_err = e
                break
    raise last_err or RuntimeError("OpenAI failed.")

def build_prompt(user_text: str, sms: bool) -> str:
    style = "very short (1–2 sentences)" if sms else "short (1–3 sentences)"
    return f"""You are a friendly, concise, bilingual (English/Spanish) fundraising assistant.
- Keep replies {style}.
- If the user asks how to donate or is ready to help, include the donation link: {FUNDRAISER_URL}
- If they greet or ask general info, explain it's for Andree Valentino (sophomore, French horn, Martin High School band) and funds support the band program.
- Match the user's language: Spanish in Spanish, English in English.
User: {user_text}
Assistant:"""

# -------------------------- FAQ router --------------------------
def faq_router(user_text: str, sms: bool = True) -> str | None:
    txt = (user_text or "").strip().lower()
    es = is_spanish(txt)

    p_money_for = [
        r"\bwhat\s+is\s+the\s+money\s+for\b", r"\bwhat\s+is\s+it\s+for\b", r"\bpurpose\b",
        r"\bpara\s+qué\s+es\s+el\s+dinero\b", r"\bpara\s+que\s+es\s+el\s+dinero\b", r"\bpara\s+qué\s+es\b",
    ]
    p_amount = [
        r"\bhow\s+much\s+should\s+i\s+donate\b", r"\bhow\s+much\b", r"\bamount\b", r"\bminimum\b",
        r"\bcu[aá]nto\s+(debo|puedo)\s+donar\b", r"\bmonto\b"
    ]
    p_mex_card = [
        r"\bmexican\s+card\b", r"\bmexico\s+card\b", r"\bworks\s+in\s+mexico\b",
        r"\btarjeta\s+mexicana\b", r"\bfunciona\s+en\s+m[eé]xico\b", r"\bacepta\s+tarjeta\b"
    ]
    p_about_andree = [
        r"\bwho\s+is\s+andree\b", r"\btell\s+me\s+about\s+andree\b",
        r"\bacerca\s+de\s+andree\b", r"\bqu[ié]n\s+es\s+andree\b"
    ]
    p_tax = [
        r"\btax\s+deductible\b", r"\btax\b", r"\breceipt\b",
        r"\bdeducible\b", r"\brecibo\b", r"\bfactura\b"
    ]
    p_deadline = [
        r"\bdeadline\b", r"\bwhen\s+does\s+it\s+end\b", r"\bby\s+when\b", r"\bgoal\b",
        r"\bmeta\b", r"\bfecha\s+l[ií]mite\b", r"\bcu[aá]ndo\s+termina\b"
    ]
    p_how_donate = [
        r"\bhow\s+do\s+i\s+donate\b", r"\bdonate\b", r"\bdonation\b", r"\blink\b",
        r"\bc[óo]mo\s+donar\b", r"\benlace\b", r"\bdonaci[oó]n\b"
    ]
    p_share = [
        r"\bcan\s+i\s+share\b", r"\bshare\b", r"\bpuedo\s+compartir\b", r"\bcompartir\b"
    ]
    p_thanks = [r"\bthanks\b", r"\bthank\s+you\b", r"\bgracias\b"]
    p_greeting = [r"\bhi\b", r"\bhello\b", r"\bhey\b", r"\bhol[ao]\b", r"\bbuenas\b"]

    def m(patts): return any(re.search(p, txt) for p in patts)

    if sms:
        if m(p_money_for):
            return (f"Funds help Martin HS band (instruments, uniforms, travel). Donate: {FUNDRAISER_URL}"
                    if not es else f"Fondo para la banda de Martin HS (instrumentos, uniformes, viajes). Dona: {FUNDRAISER_URL}")
        if m(p_amount):
            return (f"Every bit helps—$3–$5 adds up. Donate: {FUNDRAISER_URL}"
                    if not es else f"¡Todo ayuda! $3–$5 suma. Dona: {FUNDRAISER_URL}")
        if m(p_mex_card):
            return (f"Yes—most cards incl. Mexico work. Try: {FUNDRAISER_URL}"
                    if not es else f"Sí—funcionan tarjetas de México. Prueba: {FUNDRAISER_URL}")
        if m(p_about_andree):
            return (f"Andree is a Martin HS sophomore, French horn. Support: {FUNDRAISER_URL}"
                    if not es else f"Andree cursa 2º en Martin HS y toca corno. Apoya: {FUNDRAISER_URL}")
        if m(p_tax):
            return ("You’ll get an email receipt. Tax treatment varies; ask your tax advisor."
                    if not es else "Recibirás recibo por email. El tratamiento fiscal varía; consulta a tu asesor.")
        if m(p_deadline):
            if DEADLINE_HUMAN and GOAL_USD:
                return (f"Goal ${GOAL_USD}. Please donate by {DEADLINE_HUMAN}: {FUNDRAISER_URL}"
                        if not es else f"Meta ${GOAL_USD}. Dona antes del {DEADLINE_HUMAN}: {FUNDRAISER_URL}")
            return (f"Please donate when you can: {FUNDRAISER_URL}"
                    if not es else f"Dona cuando puedas: {FUNDRAISER_URL}")
        if m(p_how_donate):
            return (f"Tap to donate any amount: {FUNDRAISER_URL}"
                    if not es else f"Abre el enlace y dona: {FUNDRAISER_URL}")
        if m(p_share):
            return ("Yes, please share the link: " + FUNDRAISER_URL
                    if not es else "Sí, por favor comparte el enlace: " + FUNDRAISER_URL)
        if m(p_thanks):
            return ("Thank you! 🎺" if not es else "¡Gracias! 🎺")
        if m(p_greeting):
            return (f"Hi! Supporting Martin HS band. Donate: {FUNDRAISER_URL}"
                    if not es else f"¡Hola! Apoyamos la banda de Martin HS. Dona: {FUNDRAISER_URL}")
    return None

# -------------------------- CSV helpers --------------------------
def append_csv(path, headers, row):
    exists = os.path.exists(path)
    with open(path, "a", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=headers)
        if not exists:
            w.writeheader()
        w.writerow({k: row.get(k, "") for k in headers})

# -------------------------- BlueBubbles send --------------------------
def send_imessage(to_e164: str, body: str, attachment_url: str | None = None):
    """
    Send via BlueBubbles API (iMessage first; falls back to SMS if needed by Apple).
    """
    if not BLUEBUBBLES_URL or not BLUEBUBBLES_API_KEY:
        raise RuntimeError("Missing BLUEBUBBLES_URL / BLUEBUBBLES_API_KEY")

    payload = {
        "chatGuid": None,
        "to": [to_e164],
        "message": body,
        "method": "send-message",
        "subject": None
    }
    if attachment_url:
        payload["attachments"] = [attachment_url]

    headers = {"content-type": "application/json", "x-api-key": BLUEBUBBLES_API_KEY}
    r = requests.post(f"{BLUEBUBBLES_URL}/v1/messages", headers=headers, data=json.dumps(payload), timeout=20)
    r.raise_for_status()
    data = r.json() if r.text else {}
    append_csv(IMESSAGE_SENT_LOG,
               ["timestamp","to","body","attachment_url","bluebubbles_response"],
               {"timestamp": datetime.datetime.utcnow().isoformat(), "to": to_e164, "body": body,
                "attachment_url": attachment_url or "", "bluebubbles_response": json.dumps(data)})
    return data

# -------------------------- Flask App --------------------------
app = Flask(__name__)

@app.get("/health")
def health():
    return {
        "ok": True,
        "bluebubbles": bool(BLUEBUBBLES_URL and BLUEBUBBLES_API_KEY),
        "model_primary": PRIMARY_MODEL,
        "model_fallback": FALLBACK_MODEL,
        "fundraiser_url": FUNDRAISER_URL,
        "goal_usd": GOAL_USD,
        "deadline": DEADLINE,
        "rate_window_sec": RATE_WINDOW_SEC,
        "dup_window_sec": DUP_WINDOW_SEC,
        "vip_exclude_count": len(IMESSAGE_VIP_EXCLUDE),
    }, 200

@app.post("/imessage/webhook")
def imessage_webhook():
    """
    BlueBubbles -> VM inbound hook.
    Expected payload (varies by version), but commonly:
      { "data": { "message": { "handle": {"normalized":"+1214..."}, "text": "hi ..." , ... } } }
    """
    try:
        evt = request.get_json(force=True, silent=False)
    except Exception:
        return ("bad json", 400)

    data = (evt or {}).get("data", {})
    msg = (data.get("message") or {})
    sender = (msg.get("handle", {}) or {}).get("normalized") or ""
    text = (msg.get("text") or "") or ""

    if not sender or not text:
        return ("ok", 204)

    # Log inbound
    append_csv(IMESSAGE_INBOUND_LOG,
               ["timestamp","from","text","raw"],
               {"timestamp": datetime.datetime.utcnow().isoformat(), "from": sender, "text": text, "raw": json.dumps(evt) })

    # VIP exclusions (you will reply yourself)
    if sender in IMESSAGE_VIP_EXCLUDE:
        logging.info(f"[iMessage] VIP excluded: {sender}")
        return ("ok", 204)

    # simple guards
    if too_soon(sender) or is_duplicate(sender, text):
        logging.info(f"[iMessage] Skipping (rate/dup) from {sender}")
        return ("ok", 204)

    # Canned FAQ first (use sms=True short style)
    canned = faq_router(text, sms=True)
    if canned:
        try:
            send_imessage(sender, canned)
        except Exception as e:
            logging.exception(f"[iMessage] send failed (FAQ): {e}")
        return ("ok", 204)

    # Otherwise GPT-5
    try:
        prompt = build_prompt(text, sms=True)
        answer = ask_openai(prompt)
        if len(answer) > 500:
            answer = answer[:497] + "..."
        send_imessage(sender, answer)
    except Exception as e:
        logging.exception(f"[iMessage] OpenAI/send failed: {e}")

    return ("ok", 204)

# -------------------------- CLI (send-one / bulk-send / runserver) --------------------------
def bulk_send(csv_file: str, start_from: int, limit: int | None, dry_run: bool, image_url: str | None):
    with open(csv_file, newline="", encoding="utf-8") as f:
        rows = list(csv.DictReader(f))
    start = max(start_from, 0)
    end = len(rows) if limit is None else min(len(rows), start + limit)
    logging.info(f"Loaded {len(rows)} rows. Sending [{start}:{end}) via BlueBubbles...")

    BODY_EN = ("Hi {name} 👋 It’s Luis Carlos.\n"
               "We’re raising funds for the Martin HS Band 🎺.\n"
               "Can I send you the secure donation link?")
    BODY_ES = ("Hola {name} 👋 Soy Luis Carlos.\n"
               "Estamos recaudando fondos para la banda de Martin HS 🎺.\n"
               "¿Te puedo enviar el enlace de donación?")

    for i in range(start, end):
        row = rows[i]
        phone = row.get("Phone_E164") or row.get("Phone")
        if not phone:
            print(f"[{i}] SKIP no phone for {row.get('Name')}")
            continue
        name = first_name(row)
        lang = detect_lang_row(row)
        text = (BODY_ES if lang=="ES" else BODY_EN).format(name=name)

        try:
            if dry_run:
                print(f"[{i}] WOULD SEND to {phone} :: {text} :: img={image_url or 'none'}")
            else:
                send_imessage(phone, text, attachment_url=image_url or None)
                print(f"[{i}] SENT to {phone}")
        except Exception as e:
            print(f"[{i}] ERROR {phone}: {e}", file=sys.stderr)
        if i < end-1:
            time.sleep(DELAY_SECONDS)

def send_one(to: str, text: str, image_url: str | None):
    send_imessage(to, text, attachment_url=image_url or None)
    print(f"Sent to {to}")

def main():
    p = argparse.ArgumentParser(description="iPhone/Mac iMessage/SMS helper via BlueBubbles + GPT-5 auto-replies")
    sub = p.add_subparsers(dest="cmd", required=True)

    s_run = sub.add_parser("runserver", help="Run Flask webhook server")
    s_run.add_argument("--host", default="0.0.0.0")
    s_run.add_argument("--port", type=int, default=5000)

    s_one = sub.add_parser("send-one", help="Send a single message now")
    s_one.add_argument("--to", required=True, help="E164 phone, e.g., +12145550123")
    s_one.add_argument("--text", required=True)
    s_one.add_argument("--image-url", default="", help="Optional image URL")

    s_bulk = sub.add_parser("bulk-send", help="Send from CSV (E.164 numbers)")
    s_bulk.add_argument("csv_file")
    s_bulk.add_argument("--start-from", type=int, default=0)
    s_bulk.add_argument("--limit", type=int, default=None)
    s_bulk.add_argument("--dry-run", action="store_true")
    s_bulk.add_argument("--image-url", default="", help="Optional image URL")

    args = p.parse_args()

    if args.cmd == "runserver":
        app.run(host=args.host, port=args.port)
    elif args.cmd == "send-one":
        send_one(args.to, args.text, args.image_url or None)
    elif args.cmd == "bulk-send":
        bulk_send(args.csv_file, args.start_from, args.limit, args.dry_run, args.image_url or None)
    else:
        p.error("unknown command")

if __name__ == "__main__":
    main()
