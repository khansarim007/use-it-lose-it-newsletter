import json
import os
from urllib.parse import urlencode
from urllib.error import HTTPError
from urllib.request import Request, urlopen

from .common import normalize_subscriber_record

BASE_URL = "https://api.beehiiv.com/v2"
BEEHIIV_AUTHORIZE_URL = os.environ.get("BEEHIIV_AUTHORIZE_URL", "https://app.beehiiv.com/oauth2/authorize")
BEEHIIV_TOKEN_URL = os.environ.get("BEEHIIV_TOKEN_URL", "https://api.beehiiv.com/v2/oauth2/token")


def _request_json(url, method="GET", headers=None, data=None):
    request = Request(url, data=data, headers=headers or {}, method=method)
    with urlopen(request, timeout=30) as response:
        return json.loads(response.read().decode("utf-8"))


def build_authorize_url(client_id, redirect_uri, state, scope):
    query = urlencode(
        {
            "response_type": "code",
            "client_id": client_id,
            "redirect_uri": redirect_uri,
            "state": state,
            "scope": scope,
        }
    )
    return f"{BEEHIIV_AUTHORIZE_URL}?{query}"


def exchange_code_for_token(client_id, client_secret, code, redirect_uri):
    payload = urlencode(
        {
            "grant_type": "authorization_code",
            "client_id": client_id,
            "client_secret": client_secret,
            "code": code,
            "redirect_uri": redirect_uri,
        }
    ).encode("utf-8")
    headers = {"Content-Type": "application/x-www-form-urlencoded"}
    return _request_json(BEEHIIV_TOKEN_URL, method="POST", headers=headers, data=payload)


def _auth_headers(api_key):
    return {"Authorization": f"Bearer {api_key}"}


def fetch_publications(api_key):
    payload = _request_json(f"{BASE_URL}/publications", headers=_auth_headers(api_key))
    return payload.get("data") or payload.get("publications") or payload.get("items") or []


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
