import json
from urllib.error import HTTPError
from urllib.request import Request, urlopen

from .common import normalize_subscriber_record

BASE_URL = "https://api.beehiiv.com/v2"


def _request_json(url, method="GET", headers=None, data=None):
    request = Request(url, data=data, headers=headers or {}, method=method)
    with urlopen(request, timeout=30) as response:
        return json.loads(response.read().decode("utf-8"))


def _auth_headers(api_key):
    return {"Authorization": f"Bearer {api_key}"}


def fetch_subscribers(api_key, publication_id):
    if not publication_id:
        raise ValueError("Missing Beehiiv publication ID")

    url = f"{BASE_URL}/publications/{publication_id}/subscriptions"
    payload = _request_json(url, headers=_auth_headers(api_key))
    items = payload.get("data") or payload.get("subscriptions") or payload.get("items") or []
    normalized = []

    for subscriber in items:
        email = subscriber.get("email") or subscriber.get("email_address")
        if not email:
            continue
        last_open = subscriber.get("last_opened_at") or subscriber.get("last_open")
        engagement_score = int(subscriber.get("engagement_score") or (70 if subscriber.get("status") == "active" else 25))
        normalized.append(
            normalize_subscriber_record(
                email=email,
                last_open=last_open,
                engagement_score=engagement_score,
                source_ref=str(subscriber.get("id") or email),
                source_data={
                    "platform": "beehiiv",
                    "beehiiv_id": subscriber.get("id"),
                    "status": subscriber.get("status"),
                },
            )
        )

    return normalized


def delete_subscriber(api_key, publication_id, subscriber_id):
    if not publication_id:
        raise ValueError("Missing Beehiiv publication ID")
    if not subscriber_id:
        raise ValueError("Missing Beehiiv subscriber ID")

    url = f"{BASE_URL}/publications/{publication_id}/subscriptions/{subscriber_id}"
    request = Request(url, headers=_auth_headers(api_key), method="DELETE")
    try:
        with urlopen(request, timeout=30):
            return True
    except HTTPError as error:
        if error.code in {404, 410}:
            return False
        raise
