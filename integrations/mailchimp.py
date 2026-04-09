import base64
import hashlib
import json
from urllib.error import HTTPError
from urllib.parse import urlencode
from urllib.request import Request, urlopen

from .common import normalize_subscriber_record

MAILCHIMP_AUTH_URL = "https://login.mailchimp.com/oauth2/authorize"
MAILCHIMP_TOKEN_URL = "https://login.mailchimp.com/oauth2/token"


def _request_json(url, method="GET", headers=None, data=None):
    request = Request(url, data=data, headers=headers or {}, method=method)
    with urlopen(request, timeout=30) as response:
        return json.loads(response.read().decode("utf-8"))


def build_authorize_url(client_id, redirect_uri, state):
    query = urlencode(
        {
            "response_type": "code",
            "client_id": client_id,
            "redirect_uri": redirect_uri,
            "state": state,
        }
    )
    return f"{MAILCHIMP_AUTH_URL}?{query}"


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
    return _request_json(MAILCHIMP_TOKEN_URL, method="POST", headers=headers, data=payload)


def _auth_headers(access_token):
    token = base64.b64encode(f"anystring:{access_token}".encode("utf-8")).decode("ascii")
    return {"Authorization": f"Basic {token}"}


def fetch_subscribers(access_token, server_prefix, list_id):
    if not list_id:
        raise ValueError("Missing Mailchimp list ID")

    url = f"https://{server_prefix}.api.mailchimp.com/3.0/lists/{list_id}/members?count=1000"
    payload = _request_json(url, headers=_auth_headers(access_token))
    normalized = []

    for member in payload.get("members", []):
        email = member.get("email_address")
        if not email:
            continue
        stats = member.get("stats") or {}
        opens_count = int(stats.get("opens_count") or 0)
        avg_open_rate = stats.get("avg_open_rate")
        engagement_score = int(float(avg_open_rate) * 100) if avg_open_rate is not None else min(100, opens_count * 20)
        last_open = member.get("last_open")
        normalized.append(
            normalize_subscriber_record(
                email=email,
                last_open=last_open,
                engagement_score=engagement_score,
                source_ref=member.get("id") or email,
                source_data={
                    "platform": "mailchimp",
                    "mailchimp_id": member.get("id"),
                    "stats": stats,
                },
            )
        )

    return normalized


def delete_subscriber(access_token, server_prefix, list_id, email_address):
    if not list_id:
        raise ValueError("Missing Mailchimp list ID")
    subscriber_hash = hashlib.md5(email_address.strip().lower().encode("utf-8")).hexdigest()
    url = f"https://{server_prefix}.api.mailchimp.com/3.0/lists/{list_id}/members/{subscriber_hash}"
    request = Request(url, headers=_auth_headers(access_token), method="DELETE")
    try:
        with urlopen(request, timeout=30):
            return True
    except HTTPError as error:
        if error.code in {404, 410}:
            return False
        raise
