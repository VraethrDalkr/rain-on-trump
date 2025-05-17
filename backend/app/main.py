from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from datetime import datetime, timezone

app = FastAPI(title="Is It Raining on Trump?")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],  # ok for personal toy project
    allow_methods=["GET", "POST"],
    allow_headers=["*"],
)

# ──────────────────────────────────────────────────────────────
# Temporary stub: always returns Mar-a-Lago and "not raining".
# We'll wire in the real services in the next step.
# ──────────────────────────────────────────────────────────────
@app.get("/is_it_raining.json")
async def is_it_raining():
    return {
        "raining": False,
        "location": "Mar-a-Lago, Palm Beach FL",
        "timestamp": datetime.now(timezone.utc).isoformat(timespec="seconds"),
    }


# Placeholder routes for push-notifications (no-op for now)
@app.post("/subscribe")
async def subscribe(subscription: dict):
    """
    Receive a Web-Push subscription JSON from the frontend.
    For now we just log it; later we'll store it and use pywebpush.
    """
    print("New subscriber:", subscription)
    return {"ok": True}
