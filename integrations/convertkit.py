import json
import os
from urllib.error import HTTPError
from urllib.parse import urlencode
from urllib.request import Request, urlopen

from .common import normalize_subscriber_record

BASE_URL = "https://api.convertkit.com/v3"
BASE_URL_V4 = "https://api.kit.com/v4"
KIT_AUTHORIZE_URL = os.environ.get("CONVERTKIT_AUTHORIZE_URL", "https://api.kit.com/v4/oauth/authorize")
KIT_TOKEN_URL = os.environ.get("CONVERTKIT_TOKEN_URL", "https://api.kit.com/v4/oauth/token")


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
    return f"{KIT_AUTHORIZE_URL}?{query}"


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
    return _request_json(KIT_TOKEN_URL, method="POST", headers=headers, data=payload)


def _auth_headers(access_token=None, api_key=None):
    if access_token:
        return {"Authorization": f"Bearer {access_token}"}
    if api_key:
        return {"X-Kit-Api-Key": api_key}
    raise ValueError("Missing ConvertKit access token or API key")


def fetch_forms(access_token=None, api_key=None):
    payload = _request_json(f"{BASE_URL_V4}/forms", headers=_auth_headers(access_token=access_token, api_key=api_key))
    return payload.get("forms", [])


def fetch_subscribers(access_token=None, api_key=None, form_id=None):
    if form_id:
        url = f"{BASE_URL_V4}/forms/{form_id}/subscribers"
    else:
        url = f"{BASE_URL_V4}/subscribers"
    payload = _request_json(url, headers=_auth_headers(access_token=access_token, api_key=api_key))
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
                last_open=subscriber.get("added_at") or subscriber.get("last_open_at") or subscriber.get("created_at"),
                engagement_score=engagement_score,
                source_ref=str(subscriber.get("id") or email),
                source_data={
                    "platform": "convertkit",
                    "convertkit_id": subscriber.get("id"),
                    "form_id": form_id,
                    "tags": tags,
                    "state": subscriber.get("state"),
                },
            )
        )

    return normalized


def delete_subscriber(api_key, subscriber_id):
    if not subscriber_id:
        raise ValueError("Missing ConvertKit subscriber ID")
    url = f"{BASE_URL_V4}/subscribers/{subscriber_id}/unsubscribe"
    request = Request(url, headers=_auth_headers(api_key=api_key), data=b"", method="POST")
    try:
        with urlopen(request, timeout=30):
            return True
    except HTTPError as error:
        if error.code in {404, 410}:
            return False
        raise


def delete_subscriber_with_token(access_token, subscriber_id):
    if not subscriber_id:
        raise ValueError("Missing ConvertKit subscriber ID")
    url = f"{BASE_URL_V4}/subscribers/{subscriber_id}/unsubscribe"
    request = Request(url, headers=_auth_headers(access_token=access_token), data=b"", method="POST")
    try:
        with urlopen(request, timeout=30):
            return True
    except HTTPError as error:
        if error.code in {404, 410}:
            return False
        raise
