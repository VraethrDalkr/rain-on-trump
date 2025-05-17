# Is It Raining on Trump? ☔️🧑‍🦱

A hobby Progressive Web App that answers *“is it raining where Donald Trump is right now?”*  
– Auto-scrapes his current location → checks live precipitation → serves JSON + push notifications.

## Quick start (local)

```bash
git clone https://github.com/yourname/rain-on-trump.git
cd rain-on-trump/backend
python -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
uvicorn app.main:app --reload
