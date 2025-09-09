import os, requests
from dotenv import load_dotenv

load_dotenv()
domain = os.getenv("MAILGUN_DOMAIN")
key = os.getenv("MAILGUN_API_KEY")
sender = os.getenv("MAILGUN_FROM")
recipient = os.getenv("MAILGUN_TO")

if not all([domain, key, sender, recipient]):
    raise SystemExit("Mailgun env vars missing")

r = requests.post(
    f"https://api.mailgun.net/v3/{domain}/messages",
    auth=("api", key),
    data={"from": sender, "to": [recipient], "subject": "Mailgun test", "text": "It works."}
)
print(r.status_code, r.text[:200])