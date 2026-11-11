"""Send an Apprise JSON notification without logging the secret endpoint."""
import json
import os
from urllib.request import Request, urlopen


def alert():
    webhook = os.environ.get("FLEET_ALERT_WEBHOOK")
    if not webhook:
        print("FLEET_ALERT_WEBHOOK unset; skipping failure alert")
        return
    body = f"{os.environ['CI_PROJECT_PATH']}: scheduled pipeline failed {os.environ['CI_PIPELINE_URL']}"
    try:
        request = Request(webhook, data=json.dumps({"body": body}).encode(),
                          headers={"Content-Type": "application/json"}, method="POST")
        with urlopen(request, timeout=15) as response:
            if not 200 <= response.status < 300:
                raise ValueError("non-success response")
    except Exception:
        # urllib exceptions can include the endpoint, which may contain credentials.
        raise SystemExit("Failure alert could not be delivered") from None


if __name__ == "__main__":
    alert()
