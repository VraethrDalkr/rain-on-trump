import json, os, datetime as dt
from pathlib import Path
from typing import List
from pywebpush import webpush, WebPushException
from dotenv import load_dotenv; load_dotenv()


VAPID_PUBLIC  = os.getenv("VAPID_PUBLIC")
VAPID_PRIVATE = os.getenv("VAPID_PRIVATE")
VAPID_CLAIMS  = {"sub": "mailto:you@example.com"}

_STORE = Path(__file__).with_suffix(".subs.json")

def _load() -> List[dict]:
    if _STORE.exists():
        return json.loads(_STORE.read_text())
    return []

def _save(data: List[dict]):
    _STORE.write_text(json.dumps(data))

def add_subscription(sub: dict):
    subs = _load()
    if sub not in subs:
        subs.append(sub)
        _save(subs)

def broadcast(title: str, body: str):
    subs = _load()
    live = []
    for s in subs:
        try:
            webpush(
                subscription_info=s,
                data=json.dumps({"title": title, "body": body}),
                vapid_private_key=VAPID_PRIVATE,
                vapid_claims=VAPID_CLAIMS,
            )
            live.append(s)
        except WebPushException as e:
            print("[push] drop dead sub:", e)
    _save(live)
