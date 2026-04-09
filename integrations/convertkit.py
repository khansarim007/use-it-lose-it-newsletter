import json
from urllib.error import HTTPError
from urllib.parse import urlencode
from urllib.request import Request, urlopen

from .common import normalize_subscriber_record

BASE_URL = "https://api.convertkit.com/v3"


def _request_json(url, method="GET", headers=None, data=None):
    request = Request(url, data=data, headers=headers or {}, method=method)
    with urlopen(request, timeout=30) as response:
        return json.loads(response.read().decode("utf-8"))


def fetch_subscribers(api_key):
    url = f"{BASE_URL}/subscribers?{urlencode({'api_key': api_key})}"
    payload = _request_json(url)
    normalized = []

    for subscriber in payload.get("subscribers", []):
        email = subscriber.get("email_address") or subscriber.get("email")
        if not email:
            continue
        tags = subscriber.get("tags") or []
        engagement_score = min(100, max(0, len(tags) * 25))
        if subscriber.get("state") in {"active", "confirmed"} and engagement_score < 50:
            engagement_score = 50
        normalized.append(
            normalize_subscriber_record(
                email=email,
                last_open=subscriber.get("last_open_at") or subscriber.get("created_at"),
                engagement_score=engagement_score,
                source_ref=str(subscriber.get("id") or email),
                source_data={
                    "platform": "convertkit",
                    "convertkit_id": subscriber.get("id"),
                    "tags": tags,
                    "state": subscriber.get("state"),
                },
            )
        )

    return normalized


def delete_subscriber(api_key, subscriber_id):
    if not subscriber_id:
        raise ValueError("Missing ConvertKit subscriber ID")
    url = f"{BASE_URL}/subscribers/{subscriber_id}/unsubscribe?{urlencode({'api_key': api_key})}"
    request = Request(url, data=b"", method="POST")
    try:
        with urlopen(request, timeout=30):
            return True
    except HTTPError as error:
        if error.code in {404, 410}:
            return False
        raise
