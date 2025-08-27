#!/usr/bin/env python3
# -*- coding: utf-8 -*-

import os, sys, csv, time, json, argparse
import base64
import requests
from datetime import datetime

# ----------------------- CONFIG VIA ENV -----------------------
BB_BASE_URL   = os.getenv("BB_BASE_URL", "").rstrip("/")  # e.g. https://make-earned-ent-fu.trycloudflare.com
BB_USERNAME   = os.getenv("BB_USERNAME", "admin")         # whatever you set in BlueBubbles
BB_PASSWORD   = os.getenv("BB_PASSWORD", "")              # your BlueBubbles server password

# Message templates
BODY_EN = os.getenv("BODY_EN",
    "Hi {name} 👋 It’s Luis Carlos. We’re raising funds for the Martin HS Band 🎺. "
    "Can I send you the secure donation link?"
)
BODY_ES = os.getenv("BODY_ES",
    "Hola {name} 👋 Soy Luis Carlos. Estamos recaudando fondos para la banda de Martin HS 🎺. "
    "¿Te puedo enviar el enlace seguro para donar?"
)

# Throttle between sends (seconds)
PAUSE_SEC = float(os.getenv("PAUSE_SEC", "0.6"))

# -------------------------------------------------------------

def auth_tuple():
    if not BB_BASE_URL or not BB_PASSWORD:
        print("ERROR: Please set BB_BASE_URL and BB_PASSWORD (and optionally BB_USERNAME).", file=sys.stderr)
        sys.exit(1)
    return (BB_USERNAME, BB_PASSWORD)

def send_text(phone_e164: str, message: str):
    """
    Sends a message to a phone number via BlueBubbles.
    We use the 'addresses' form so we don't need a chat GUID beforehand.
    """
    url = f"{BB_BASE_URL}/api/v1/message/send"
    payload = {
        "addresses": [phone_e164],   # e.g., "+12142354360"
        "message": message
    }
    r = requests.post(url, auth=auth_tuple(), json=payload, timeout=20)
    if r.status_code >= 300:
        raise RuntimeError(f"HTTP {r.status_code}: {r.text}")
    return r.json()

def detect_lang(row) -> str:
    """
    Returns 'EN' or 'ES' based on CSV columns.
    Priority: Language column; else Country==MX => ES; otherwise EN.
    """
    lang_raw = (row.get("Language") or row.get("language") or "").strip().lower()
    if lang_raw in ("es","es-mx","spanish","español"):
        return "ES"
    if lang_raw in ("en","en-us","english"):
        return "EN"
    country = (row.get("Country") or "").strip().upper()
    return "ES" if country == "MX" else "EN"

def first_name(row) -> str:
    return (row.get("GreetingName") or row.get("FirstName") or row.get("Name") or "friend").strip()

def load_rows(csv_path: str):
    with open(csv_path, newline="", encoding="utf-8") as f:
        return list(csv.DictReader(f))

def main():
    ap = argparse.ArgumentParser(description="Send messages via your iPhone/Mac using BlueBubbles")
    ap.add_argument("--to", help="Single E164 number to send to, e.g. +12142354360")
    ap.add_argument("--csv", help="CSV with columns: Phone_E164 or Phone, Name/FirstName/GreetingName, Language/Country")
    ap.add_argument("--limit", type=int, default=None, help="Max rows to send (when using --csv)")
    ap.add_argument("--start-from", type=int, default=0, help="Start index in CSV (0-based)")
    ap.add_argument("--lang", choices=["EN","ES","AUTO"], default="AUTO", help="Force language; default AUTO")
    ap.add_argument("--dry-run", action="store_true", help="Print actions but do not send")
    args = ap.parse_args()

    if not args.to and not args.csv:
        print("Provide --to +E164 or --csv path", file=sys.stderr)
        sys.exit(1)

    # Single send
    if args.to:
        name = "friend"
        lang = "EN" if args.lang != "AUTO" else "EN"
        body = (BODY_ES if lang=="ES" else BODY_EN).format(name=name)
        if args.dry_run:
            print(f"DRY-RUN to {args.to} :: {body}")
        else:
            resp = send_text(args.to, body)
            print(f"Sent to {args.to} :: {json.dumps(resp)}")
        return

    # CSV send
    rows = load_rows(args.csv)
    start = max(args.start_from, 0)
    end = len(rows) if args.limit is None else min(len(rows), start + args.limit)
    print(f"Loaded {len(rows)} rows from {args.csv}. Sending rows [{start}:{end})...")

    sent = 0
    for idx in range(start, end):
        row = rows[idx]
        phone = row.get("Phone_E164") or row.get("Phone")
        if not phone:
            print(f"[{idx}] SKIP: No phone for {row.get('Name')}")
            continue

        name = first_name(row)
        lang = detect_lang(row) if args.lang == "AUTO" else args.lang
        body = (BODY_ES if lang=="ES" else BODY_EN).format(name=name)

        try:
            if args.dry_run:
                print(f"[{idx}] DRY-RUN to {phone} ({lang}) :: {body}")
            else:
                resp = send_text(phone, body)
                print(f"[{idx}] Sent to {phone} ({lang}) :: id={resp.get('identifier') or resp}")
                sent += 1
            if idx < end - 1:
                time.sleep(PAUSE_SEC)
        except Exception as e:
            print(f"[{idx}] ERROR sending to {phone}: {e}", file=sys.stderr)

    print(f"Done. Attempted: {end-start}, Sent (no exception): {sent}")

if __name__ == "__main__":
    main()
