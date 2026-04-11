import csv
import io
import json
import os
import re
import secrets
import sqlite3
from datetime import datetime, timedelta
from functools import wraps
from urllib.parse import quote, unquote, urlparse

import bcrypt
from flask import (
    Flask,
    abort,
    flash,
    g,
    jsonify,
    redirect,
    render_template,
    request,
    send_file,
    session,
    url_for,
)

from integrations.beehiiv import delete_subscriber as delete_beehiiv_subscriber
from integrations.beehiiv import build_authorize_url as build_beehiiv_authorize_url
from integrations.beehiiv import exchange_code_for_token as exchange_beehiiv_code
from integrations.beehiiv import fetch_publications as fetch_beehiiv_publications
from integrations.beehiiv import fetch_subscribers as fetch_beehiiv_subscribers
from integrations.common import parse_iso_datetime, safe_json_dumps, safe_json_loads
from integrations.convertkit import delete_subscriber as delete_convertkit_subscriber
from integrations.convertkit import delete_subscriber_with_token as delete_convertkit_subscriber_with_token
from integrations.convertkit import build_authorize_url as build_convertkit_authorize_url
from integrations.convertkit import exchange_code_for_token as exchange_convertkit_code
from integrations.convertkit import fetch_forms as fetch_convertkit_forms
from integrations.convertkit import fetch_subscribers as fetch_convertkit_subscribers
from integrations.mailchimp import build_authorize_url as build_mailchimp_authorize_url
from integrations.mailchimp import delete_subscriber as delete_mailchimp_subscriber
from integrations.mailchimp import exchange_code_for_token as exchange_mailchimp_code
from integrations.mailchimp import fetch_lists as fetch_mailchimp_lists
from integrations.mailchimp import fetch_oauth_metadata as fetch_mailchimp_metadata
from integrations.mailchimp import fetch_subscribers as fetch_mailchimp_subscribers


app = Flask(__name__)
app.config["SECRET_KEY"] = os.environ.get("SECRET_KEY", "dev-secret-change-in-production")
DATABASE = "database.db"
DB_READY = False


PLATFORM_CONFIG = {
    "mailchimp": {
        "label": "Mailchimp",
        "connect_label": "Connect Mailchimp",
        "description": "Sync subscribers from Mailchimp instead of uploading CSVs.",
    },
    "convertkit": {
        "label": "ConvertKit",
        "connect_label": "Connect ConvertKit",
        "description": "Connect ConvertKit with OAuth and sync subscribers without API keys.",
    },
    "beehiiv": {
        "label": "Beehiiv",
        "connect_label": "Connect Beehiiv",
        "description": "Connect Beehiiv with OAuth and sync subscribers automatically.",
    },
}


def get_db():
    if "db" not in g:
        g.db = sqlite3.connect(DATABASE)
        g.db.row_factory = sqlite3.Row
    return g.db


@app.teardown_appcontext
def close_db(exception):
    db = g.pop("db", None)
    if db is not None:
        db.close()


def init_db():
    db = get_db()
    db.executescript(
        """
        CREATE TABLE IF NOT EXISTS users (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            email TEXT UNIQUE NOT NULL,
            password_hash TEXT NOT NULL
        );

        CREATE TABLE IF NOT EXISTS subscribers (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER NOT NULL,
            email TEXT NOT NULL,
            status TEXT NOT NULL DEFAULT 'active',
            last_opened_at TEXT,
            last_clicked_at TEXT,
            created_at TEXT NOT NULL,
            FOREIGN KEY (user_id) REFERENCES users(id)
        );

        CREATE TABLE IF NOT EXISTS emails (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER NOT NULL,
            subject TEXT NOT NULL,
            body TEXT NOT NULL,
            created_at TEXT NOT NULL,
            FOREIGN KEY (user_id) REFERENCES users(id)
        );

        CREATE TABLE IF NOT EXISTS integrations (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER NOT NULL,
            platform TEXT NOT NULL,
            access_token TEXT,
            api_key TEXT,
            extra_data TEXT,
            created_at TEXT NOT NULL,
            FOREIGN KEY (user_id) REFERENCES users(id)
        );

        CREATE TABLE IF NOT EXISTS auto_clean_settings (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER UNIQUE NOT NULL,
            enabled INTEGER NOT NULL DEFAULT 0,
            frequency TEXT NOT NULL DEFAULT 'weekly',
            last_run_at TEXT,
            pending_review INTEGER NOT NULL DEFAULT 0,
            FOREIGN KEY (user_id) REFERENCES users(id)
        );

        CREATE TABLE IF NOT EXISTS pending_clean (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER NOT NULL,
            total_subscribers INTEGER NOT NULL DEFAULT 0,
            inactive_count INTEGER NOT NULL DEFAULT 0,
            data_snapshot TEXT NOT NULL,
            created_at TEXT NOT NULL,
            status TEXT NOT NULL DEFAULT 'pending',
            FOREIGN KEY (user_id) REFERENCES users(id)
        );
        """
    )
    ensure_db_columns(db)
    db.commit()


def ensure_db_columns(db):
    subscriber_columns = {row["name"] for row in db.execute("PRAGMA table_info(subscribers)").fetchall()}
    required_columns = {
        "source_platform": "TEXT",
        "source_ref": "TEXT",
        "source_data": "TEXT",
        "last_synced_at": "TEXT",
    }
    for column, column_type in required_columns.items():
        if column not in subscriber_columns:
            db.execute(f"ALTER TABLE subscribers ADD COLUMN {column} {column_type}")

    integration_columns = {row["name"] for row in db.execute("PRAGMA table_info(integrations)").fetchall()}
    if "extra_data" not in integration_columns:
        db.execute("ALTER TABLE integrations ADD COLUMN extra_data TEXT")


@app.before_request
def ensure_database_ready():
    global DB_READY
    if DB_READY:
        return
    init_db()
    DB_READY = True


def parse_datetime(value):
    return parse_iso_datetime(value)


def classify_engagement(last_opened_at=None, last_clicked_at=None, source_data=None):
    data = source_data or {}
    engagement_score = data.get("engagement_score")
    if engagement_score is not None:
        score = int(engagement_score)
        if score >= 70:
            return "active"
        if score >= 40:
            return "inactive"
        return "removed"

    removed_cutoff = datetime.utcnow() - timedelta(days=14)
    inactive_cutoff = datetime.utcnow() - timedelta(days=7)
    last_clicked = parse_datetime(last_clicked_at)
    last_opened = parse_datetime(last_opened_at)

    if not last_clicked or last_clicked < removed_cutoff:
        return "removed"
    if not last_opened or last_opened < inactive_cutoff:
        return "inactive"
    return "active"


def get_integrations(user_id):
    db = get_db()
    rows = db.execute(
        "SELECT * FROM integrations WHERE user_id = ? ORDER BY created_at DESC",
        (user_id,),
    ).fetchall()
    integrations = {}
    for row in rows:
        item = dict(row)
        item["extra_data"] = safe_json_loads(item.get("extra_data"), {})
        integrations[item["platform"]] = item
    return integrations


def upsert_integration(user_id, platform, access_token=None, api_key=None, extra_data=None):
    db = get_db()
    existing = db.execute(
        "SELECT id FROM integrations WHERE user_id = ? AND platform = ?",
        (user_id, platform),
    ).fetchone()
    payload = (
        access_token,
        api_key,
        safe_json_dumps(extra_data or {}),
        now_iso(),
        user_id,
        platform,
    )

    if existing:
        db.execute(
            """
            UPDATE integrations
            SET access_token = ?, api_key = ?, extra_data = ?, created_at = ?
            WHERE user_id = ? AND platform = ?
            """,
            payload,
        )
    else:
        db.execute(
            """
            INSERT INTO integrations (access_token, api_key, extra_data, created_at, user_id, platform)
            VALUES (?, ?, ?, ?, ?, ?)
            """,
            payload,
        )
    db.commit()


def remove_integration(user_id, platform):
    db = get_db()
    db.execute(
        "DELETE FROM integrations WHERE user_id = ? AND platform = ?",
        (user_id, platform),
    )
    db.commit()


def get_integration(user_id, platform):
    db = get_db()
    row = db.execute(
        "SELECT * FROM integrations WHERE user_id = ? AND platform = ?",
        (user_id, platform),
    ).fetchone()
    if not row:
        return None
    item = dict(row)
    item["extra_data"] = safe_json_loads(item.get("extra_data"), {})
    return item


def has_connected_platform(user_id):
    db = get_db()
    row = db.execute(
        "SELECT COUNT(*) AS count FROM integrations WHERE user_id = ?",
        (user_id,),
    ).fetchone()
    return (row["count"] or 0) > 0


def get_auto_clean_settings(user_id):
    db = get_db()
    row = db.execute(
        "SELECT * FROM auto_clean_settings WHERE user_id = ?",
        (user_id,),
    ).fetchone()
    if not row:
        return {
            "user_id": user_id,
            "enabled": 0,
            "frequency": "weekly",
            "last_run_at": None,
            "pending_review": 0,
        }
    item = dict(row)
    item["enabled"] = int(item.get("enabled") or 0)
    item["pending_review"] = int(item.get("pending_review") or 0)
    return item


def upsert_auto_clean_settings(user_id, enabled, frequency, last_run_at=None, pending_review=None):
    db = get_db()
    current = get_auto_clean_settings(user_id)
    if frequency not in {"daily", "weekly", "monthly"}:
        frequency = "weekly"

    final_last_run = last_run_at if last_run_at is not None else current.get("last_run_at")
    final_pending_review = int(pending_review) if pending_review is not None else int(current.get("pending_review") or 0)

    existing = db.execute(
        "SELECT id FROM auto_clean_settings WHERE user_id = ?",
        (user_id,),
    ).fetchone()

    payload = (
        int(bool(enabled)),
        frequency,
        final_last_run,
        final_pending_review,
        user_id,
    )

    if existing:
        db.execute(
            """
            UPDATE auto_clean_settings
            SET enabled = ?, frequency = ?, last_run_at = ?, pending_review = ?
            WHERE user_id = ?
            """,
            payload,
        )
    else:
        db.execute(
            """
            INSERT INTO auto_clean_settings (enabled, frequency, last_run_at, pending_review, user_id)
            VALUES (?, ?, ?, ?, ?)
            """,
            payload,
        )
    db.commit()


def get_latest_pending_clean(user_id):
    db = get_db()
    row = db.execute(
        """
        SELECT *
        FROM pending_clean
        WHERE user_id = ? AND status = 'pending'
        ORDER BY created_at DESC
        LIMIT 1
        """,
        (user_id,),
    ).fetchone()
    if not row:
        return None
    item = dict(row)
    item["data_snapshot"] = safe_json_loads(item.get("data_snapshot"), [])
    return item


def get_latest_clean_report(user_id):
    db = get_db()
    row = db.execute(
        """
        SELECT *
        FROM pending_clean
        WHERE user_id = ?
        ORDER BY created_at DESC
        LIMIT 1
        """,
        (user_id,),
    ).fetchone()
    if not row:
        return None
    item = dict(row)
    item["data_snapshot"] = safe_json_loads(item.get("data_snapshot"), [])
    return item


def get_pending_clean(user_id, pending_id):
    db = get_db()
    row = db.execute(
        "SELECT * FROM pending_clean WHERE id = ? AND user_id = ?",
        (pending_id, user_id),
    ).fetchone()
    if not row:
        return None
    item = dict(row)
    item["data_snapshot"] = safe_json_loads(item.get("data_snapshot"), [])
    return item


def create_pending_clean(user_id, total_subscribers, inactive_users):
    if not inactive_users:
        return None

    db = get_db()
    existing = get_latest_pending_clean(user_id)
    if existing:
        return existing

    created_at = now_iso()
    db.execute(
        """
        INSERT INTO pending_clean (
            user_id, total_subscribers, inactive_count, data_snapshot, created_at, status
        ) VALUES (?, ?, ?, ?, ?, 'pending')
        """,
        (
            user_id,
            int(total_subscribers or 0),
            len(inactive_users),
            safe_json_dumps(inactive_users),
            created_at,
        ),
    )
    db.commit()
    return get_latest_pending_clean(user_id)


def send_optional_auto_clean_notification(user_email, inactive_count):
    if os.environ.get("AUTO_CLEAN_NOTIFY_ENABLED", "0") != "1":
        return
    print(f"[AUTO-CLEAN REPORT] To: {user_email} | Subject: Your CullList report is ready | Body: You have {inactive_count} inactive subscribers ready to clean.")


def is_due_for_auto_clean(last_run_at, frequency):
    if not last_run_at:
        return True
    last = parse_datetime(last_run_at)
    if not last:
        return True

    now = datetime.utcnow()
    if frequency == "daily":
        return now - last >= timedelta(days=1)
    if frequency == "weekly":
        return now - last >= timedelta(days=7)
    if frequency == "monthly":
        return now - last >= timedelta(days=30)
    return now - last >= timedelta(days=7)


def build_inactive_snapshot(user_id):
    db = get_db()
    integrations = get_integrations(user_id)
    connected_platforms = list(integrations.keys())
    if not connected_platforms:
        return 0, []

    total_subscribers = db.execute(
        """
        SELECT COUNT(*) AS count
        FROM subscribers
        WHERE user_id = ? AND source_platform IN ({})
        """.format(",".join(["?" for _ in connected_platforms])),
        (user_id, *connected_platforms),
    ).fetchone()["count"] or 0

    snapshot = []
    for platform in connected_platforms:
        rows = db.execute(
            """
            SELECT email, source_ref, source_data
            FROM subscribers
            WHERE user_id = ? AND source_platform = ? AND status = 'inactive'
            ORDER BY email ASC
            """,
            (user_id, platform),
        ).fetchall()
        for row in rows:
            snapshot.append(
                {
                    "platform": platform,
                    "email": row["email"],
                    "source_ref": row["source_ref"],
                    "source_data": safe_json_loads(row["source_data"], {}),
                }
            )

    return total_subscribers, snapshot


def run_auto_clean_for_user(user_id):
    settings = get_auto_clean_settings(user_id)
    if not int(settings.get("enabled") or 0):
        return None

    if not has_connected_platform(user_id):
        upsert_auto_clean_settings(user_id, enabled=0, frequency=settings.get("frequency", "weekly"), pending_review=0)
        return None

    if not is_due_for_auto_clean(settings.get("last_run_at"), settings.get("frequency", "weekly")):
        return None

    integrations = get_integrations(user_id)
    for platform in integrations:
        try:
            sync_platform(user_id, platform)
        except Exception:
            continue

    total, inactive_snapshot = build_inactive_snapshot(user_id)
    existing_pending = get_latest_pending_clean(user_id)
    pending = existing_pending or create_pending_clean(user_id, total, inactive_snapshot)
    if pending and not existing_pending:
        db = get_db()
        user = db.execute("SELECT email FROM users WHERE id = ?", (user_id,)).fetchone()
        if user:
            send_optional_auto_clean_notification(user["email"], pending.get("inactive_count", 0))
    upsert_auto_clean_settings(
        user_id,
        enabled=1,
        frequency=settings.get("frequency", "weekly"),
        last_run_at=now_iso(),
        pending_review=1 if pending else 0,
    )
    return pending


def run_auto_clean_job():
    db = get_db()
    rows = db.execute(
        "SELECT user_id FROM auto_clean_settings WHERE enabled = 1",
    ).fetchall()
    created = 0
    processed = 0
    for row in rows:
        processed += 1
        pending = run_auto_clean_for_user(row["user_id"])
        if pending:
            created += 1
    return {"processed": processed, "created": created}


def delete_snapshot_item(user_id, item):
    platform = item.get("platform")
    email = (item.get("email") or "").strip().lower()
    source_ref = item.get("source_ref")
    source_data = item.get("source_data") or {}

    integration = get_integration(user_id, platform)
    if not integration:
        return False

    if platform == "mailchimp":
        extra = integration.get("extra_data") or {}
        delete_mailchimp_subscriber(
            integration.get("access_token"),
            extra.get("server_prefix"),
            extra.get("list_id") or os.environ.get("MAILCHIMP_LIST_ID"),
            email,
        )
    elif platform == "convertkit":
        subscriber_id = source_data.get("convertkit_id") or source_ref
        if integration.get("access_token"):
            delete_convertkit_subscriber_with_token(integration.get("access_token"), subscriber_id)
        else:
            delete_convertkit_subscriber(integration.get("api_key"), subscriber_id)
    elif platform == "beehiiv":
        publication_id = integration.get("extra_data", {}).get("publication_id") or os.environ.get("BEEHIIV_PUBLICATION_ID")
        token = integration.get("access_token") or integration.get("api_key")
        delete_beehiiv_subscriber(token, publication_id, source_data.get("beehiiv_id") or source_ref)
    else:
        return False

    db = get_db()
    db.execute(
        """
        UPDATE subscribers
        SET status = 'removed'
        WHERE user_id = ? AND source_platform = ? AND source_ref = ?
        """,
        (user_id, platform, source_ref),
    )
    db.commit()
    return True


def update_pending_status(pending_id, user_id, status):
    db = get_db()
    db.execute(
        "UPDATE pending_clean SET status = ? WHERE id = ? AND user_id = ?",
        (status, pending_id, user_id),
    )
    db.commit()


def build_platform_cards(user_id):
    db = get_db()
    integrations = get_integrations(user_id)
    cards = []
    for platform, meta in PLATFORM_CONFIG.items():
        integration = integrations.get(platform)
        counts = db.execute(
            """
            SELECT
                COUNT(*) AS total,
                SUM(CASE WHEN status IN ('inactive', 'removed') THEN 1 ELSE 0 END) AS removable
            FROM subscribers
            WHERE user_id = ? AND source_platform = ?
            """,
            (user_id, platform),
        ).fetchone()
        cards.append(
            {
                "platform": platform,
                "label": meta["label"],
                "description": meta["description"],
                "connect_label": meta["connect_label"],
                "connected": integration is not None,
                "connected_at": integration["created_at"] if integration else None,
                "subscriber_count": counts["total"] or 0,
                "removable_count": counts["removable"] or 0,
            }
        )
    return cards


def fetch_platform_subscribers(user_id, platform):
    integration = get_integration(user_id, platform)
    if not integration:
        raise ValueError(f"{PLATFORM_CONFIG[platform]['label']} is not connected")

    if platform == "mailchimp":
        extra = integration.get("extra_data") or {}
        server_prefix = extra.get("server_prefix")
        list_id = extra.get("list_id") or os.environ.get("MAILCHIMP_LIST_ID")
        if not list_id and server_prefix and integration.get("access_token"):
            lists = fetch_mailchimp_lists(integration["access_token"], server_prefix)
            if lists:
                list_id = lists[0].get("id")
                extra["list_id"] = list_id
                upsert_integration(
                    user_id,
                    "mailchimp",
                    access_token=integration.get("access_token"),
                    api_key=integration.get("api_key"),
                    extra_data=extra,
                )
        return fetch_mailchimp_subscribers(integration["access_token"], server_prefix, list_id)

    if platform == "convertkit":
        access_token = integration.get("access_token")
        api_key = integration.get("api_key")
        form_id = integration.get("extra_data", {}).get("form_id")
        return fetch_convertkit_subscribers(access_token=access_token, api_key=api_key, form_id=form_id)

    if platform == "beehiiv":
        access_token = integration.get("access_token")
        api_key = integration.get("api_key")
        publication_id = integration.get("extra_data", {}).get("publication_id") or os.environ.get("BEEHIIV_PUBLICATION_ID")
        return fetch_beehiiv_subscribers(access_token or api_key, publication_id)

    raise ValueError("Unsupported platform")


def upsert_platform_subscribers(user_id, platform, records, initial_sync=False):
    db = get_db()
    inserted = 0
    updated = 0

    for record in records:
        email = (record.get("email") or "").strip().lower()
        if not is_valid_email(email):
            continue

        source_ref = record.get("source_ref") or email
        source_data = record.get("source_data") or {}
        # For initial sync, mark new records as active; otherwise classify engagement
        status = "active" if initial_sync else classify_engagement(record.get("last_open"), None, source_data)
        last_open = record.get("last_open")
        engagement_score = source_data.get("engagement_score")
        last_clicked = last_open if engagement_score is not None and int(engagement_score) >= 70 else None

        existing = db.execute(
            "SELECT id FROM subscribers WHERE user_id = ? AND source_platform = ? AND source_ref = ?",
            (user_id, platform, source_ref),
        ).fetchone()
        payload = (
            email,
            status,
            last_open,
            last_clicked,
            safe_json_dumps(source_data),
            now_iso(),
            user_id,
            platform,
            source_ref,
        )

        if existing:
            db.execute(
                """
                UPDATE subscribers
                SET email = ?, status = ?, last_opened_at = ?, last_clicked_at = ?,
                    source_data = ?, last_synced_at = ?
                WHERE user_id = ? AND source_platform = ? AND source_ref = ?
                """,
                payload,
            )
            updated += 1
        else:
            db.execute(
                """
                INSERT INTO subscribers (
                    user_id, email, status, last_opened_at, last_clicked_at,
                    created_at, source_platform, source_ref, source_data, last_synced_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    user_id,
                    email,
                    status,
                    last_open,
                    last_clicked,
                    now_iso(),
                    platform,
                    source_ref,
                    safe_json_dumps(source_data),
                    now_iso(),
                ),
            )
            inserted += 1

    db.commit()
    return inserted, updated


def sync_platform(user_id, platform, initial_sync=False):
    records = fetch_platform_subscribers(user_id, platform)
    return upsert_platform_subscribers(user_id, platform, records, initial_sync=initial_sync)


def sync_all_platforms(user_id):
    totals = {}
    for platform in PLATFORM_CONFIG:
        integration = get_integration(user_id, platform)
        if not integration:
            continue
        inserted, updated = sync_platform(user_id, platform)
        totals[platform] = {"inserted": inserted, "updated": updated}
    return totals


def get_platform_cleanup_count(user_id, platform):
    db = get_db()
    row = db.execute(
        """
        SELECT COUNT(*) AS count
        FROM subscribers
        WHERE user_id = ? AND source_platform = ? AND status IN ('inactive', 'removed')
        """,
        (user_id, platform),
    ).fetchone()
    return row["count"] or 0


def cleanup_platform_subscribers(user_id, platform):
    integration = get_integration(user_id, platform)
    if not integration:
        raise ValueError(f"{PLATFORM_CONFIG[platform]['label']} is not connected")

    db = get_db()
    rows = db.execute(
        """
        SELECT id, email, source_ref, source_data
        FROM subscribers
        WHERE user_id = ? AND source_platform = ? AND status IN ('inactive', 'removed')
        """,
        (user_id, platform),
    ).fetchall()

    removed = 0
    for row in rows:
        source_data = safe_json_loads(row["source_data"], {})
        if platform == "mailchimp":
            extra = integration.get("extra_data") or {}
            server_prefix = extra.get("server_prefix")
            list_id = extra.get("list_id") or os.environ.get("MAILCHIMP_LIST_ID")
            delete_mailchimp_subscriber(integration["access_token"], server_prefix, list_id, row["email"])
        elif platform == "convertkit":
            subscriber_id = source_data.get("convertkit_id") or row["source_ref"]
            if integration.get("access_token"):
                delete_convertkit_subscriber_with_token(integration.get("access_token"), subscriber_id)
            else:
                delete_convertkit_subscriber(integration.get("api_key"), subscriber_id)
        elif platform == "beehiiv":
            publication_id = integration.get("extra_data", {}).get("publication_id") or os.environ.get("BEEHIIV_PUBLICATION_ID")
            token = integration.get("access_token") or integration.get("api_key")
            delete_beehiiv_subscriber(token, publication_id, source_data.get("beehiiv_id") or row["source_ref"])
        else:
            raise ValueError("Unsupported platform")

        db.execute(
            "UPDATE subscribers SET status = 'removed' WHERE id = ?",
            (row["id"],),
        )
        removed += 1

    db.commit()
    return removed


def classify_and_update_all_subscribers(user_id):
    db = get_db()
    rows = db.execute(
        "SELECT id, last_opened_at, last_clicked_at, source_data FROM subscribers WHERE user_id = ?",
        (user_id,),
    ).fetchall()

    for row in rows:
        source_data = safe_json_loads(row["source_data"], {})
        status = classify_engagement(row["last_opened_at"], row["last_clicked_at"], source_data)
        db.execute("UPDATE subscribers SET status = ? WHERE id = ?", (status, row["id"]))

    db.commit()


def login_required(view_func):
    @wraps(view_func)
    def wrapped(*args, **kwargs):
        if "user_id" not in session:
            return redirect(url_for("login"))
        return view_func(*args, **kwargs)

    return wrapped


def current_user():
    user_id = session.get("user_id")
    if not user_id:
        return None
    db = get_db()
    return db.execute("SELECT * FROM users WHERE id = ?", (user_id,)).fetchone()


def is_valid_email(email):
    return bool(re.fullmatch(r"[^@\s]+@[^@\s]+\.[^@\s]+", email or ""))


def password_validation_error(password):
    if len(password or "") < 8:
        return "Password must be at least 8 characters long."
    if not re.search(r"[a-z]", password):
        return "Password must include at least one lowercase letter."
    if not re.search(r"[A-Z]", password):
        return "Password must include at least one uppercase letter."
    if not re.search(r"\d", password):
        return "Password must include at least one number."
    if not re.search(r"[^A-Za-z0-9]", password):
        return "Password must include at least one special character."
    return None


def now_iso():
    return datetime.utcnow().isoformat()


def is_safe_redirect_url(url):
    parsed = urlparse(url)
    return parsed.scheme in {"http", "https"} and bool(parsed.netloc)


def rewrite_links_with_tracking(body_html, subscriber_id):
    def replace_href(match):
        original_url = match.group(1)
        if not is_safe_redirect_url(original_url):
            return match.group(0)
        tracked = url_for("track_click", subscriber_id=subscriber_id, url=quote(original_url, safe=""), _external=True)
        return f'href="{tracked}"'

    return re.sub(r'href=["\']([^"\']+)["\']', replace_href, body_html, flags=re.IGNORECASE)


def inject_tracking_pixel(body_html, subscriber_id):
    pixel_url = url_for("track_open", subscriber_id=subscriber_id, _external=True)
    pixel_tag = (
        f'<img src="{pixel_url}" alt="" width="1" height="1" '
        'style="display:none;width:1px;height:1px;" />'
    )
    return f"{body_html}\n{pixel_tag}"


def apply_engagement_rules(user_id):
    classify_and_update_all_subscribers(user_id)


@app.route("/")
def landing():
    if session.get("user_id"):
        return redirect(url_for("dashboard"))
    return render_template("landing.html")


@app.route("/signup", methods=["GET", "POST"])
def signup():
    if session.get("user_id"):
        return redirect(url_for("dashboard"))

    if request.method == "POST":
        email = request.form.get("email", "").strip().lower()
        password = request.form.get("password", "")

        if not is_valid_email(email):
            flash("Please provide a valid email address.", "error")
            return render_template("signup.html")

        password_error = password_validation_error(password)
        if password_error:
            flash(password_error, "error")
            return render_template("signup.html")

        password_hash = bcrypt.hashpw(password.encode("utf-8"), bcrypt.gensalt()).decode("utf-8")

        db = get_db()
        try:
            db.execute(
                "INSERT INTO users (email, password_hash) VALUES (?, ?)",
                (email, password_hash),
            )
            db.commit()
            flash("Account created. Please log in.", "success")
            return redirect(url_for("login"))
        except sqlite3.IntegrityError:
            flash("Email already registered.", "error")

    return render_template("signup.html")


@app.route("/login", methods=["GET", "POST"])
def login():
    if session.get("user_id"):
        return redirect(url_for("dashboard"))

    if request.method == "POST":
        email = request.form.get("email", "").strip().lower()
        password = request.form.get("password", "")

        db = get_db()
        user = db.execute("SELECT * FROM users WHERE email = ?", (email,)).fetchone()
        if not user:
            flash("Invalid credentials.", "error")
            return render_template("login.html")

        if not bcrypt.checkpw(password.encode("utf-8"), user["password_hash"].encode("utf-8")):
            flash("Invalid credentials.", "error")
            return render_template("login.html")

        session["user_id"] = user["id"]
        flash("Logged in successfully.", "success")
        return redirect(url_for("dashboard"))

    return render_template("login.html")


@app.route("/logout")
def logout():
    session.clear()
    return redirect(url_for("landing"))


@app.route("/dashboard")
@login_required
def dashboard():
    user = current_user()
    db = get_db()

    stats = db.execute(
        """
        SELECT
            COUNT(*) AS total,
            SUM(CASE WHEN status = 'active' THEN 1 ELSE 0 END) AS active,
            SUM(CASE WHEN status = 'inactive' THEN 1 ELSE 0 END) AS inactive,
            SUM(CASE WHEN status = 'removed' THEN 1 ELSE 0 END) AS removed,
            SUM(CASE WHEN last_opened_at IS NOT NULL THEN 1 ELSE 0 END) AS opened,
            SUM(CASE WHEN last_clicked_at IS NOT NULL THEN 1 ELSE 0 END) AS clicked
        FROM subscribers
        WHERE user_id = ?
        """,
        (user["id"],),
    ).fetchone()

    total = stats["total"] or 0
    active_count = stats["active"] or 0
    inactive_count = stats["inactive"] or 0
    removed_count = stats["removed"] or 0
    inactive_total = inactive_count + removed_count
    open_rate = (stats["opened"] / total * 100) if total else 0
    click_rate = (stats["clicked"] / total * 100) if total else 0
    engagement_percent = (active_count / total * 100) if total else 0
    never_read_percent = (inactive_total / total * 100) if total else 0
    platform_cards = build_platform_cards(user["id"])
    auto_clean_settings = get_auto_clean_settings(user["id"])
    pending_clean = get_latest_pending_clean(user["id"])
    latest_clean_report = get_latest_clean_report(user["id"])
    integrations_connected = has_connected_platform(user["id"])

    first_connected_platform = None
    for card in platform_cards:
        if card["connected"]:
            first_connected_platform = card["platform"]
            break

    cleaned_count = request.args.get("cleaned", type=int)
    engaged_readers = request.args.get("engaged", type=int)

    return render_template(
        "dashboard.html",
        user=user,
        stats=stats,
        total=total,
        active_count=active_count,
        inactive_count=inactive_total,
        engagement_percent=round(engagement_percent, 2),
        never_read_percent=round(never_read_percent, 2),
        open_rate=round(open_rate, 2),
        click_rate=round(click_rate, 2),
        has_data=total > 0,
        before_total=total,
        before_open_rate=round(open_rate, 2),
        after_total=active_count,
        after_open_rate=round((stats["opened"] / active_count * 100), 2) if active_count else 0,
        first_connected_platform=first_connected_platform,
        cleaned_count=cleaned_count,
        engaged_readers=engaged_readers,
        platform_cards=platform_cards,
        auto_clean_settings=auto_clean_settings,
        pending_clean=pending_clean,
        latest_clean_report=latest_clean_report,
        integrations_connected=integrations_connected,
    )


@app.route("/settings")
@login_required
def settings():
    return redirect(url_for("dashboard") + "#settings")


@app.route("/auto-clean/settings", methods=["POST"])
@login_required
def update_auto_clean_settings_route():
    user = current_user()
    enabled = request.form.get("enabled") == "on"
    frequency = (request.form.get("frequency") or "weekly").strip().lower()
    if frequency not in {"daily", "weekly", "monthly"}:
        frequency = "weekly"

    if enabled and not has_connected_platform(user["id"]):
        upsert_auto_clean_settings(user["id"], enabled=0, frequency=frequency, pending_review=0)
        flash("Connect your platform to enable Auto-Clean.", "error")
        return redirect(url_for("dashboard"))

    upsert_auto_clean_settings(user["id"], enabled=1 if enabled else 0, frequency=frequency)
    flash("Auto-Clean settings saved.", "success")
    return redirect(url_for("dashboard"))


@app.route("/connect/mailchimp")
@login_required
def connect_mailchimp():
    user = current_user()
    code = request.args.get("code")
    state = request.args.get("state")

    if code:
        expected_state = session.pop("mailchimp_state", None)
        if not expected_state or state != expected_state:
            flash("Mailchimp connection failed. Please try again.", "error")
            return redirect(url_for("dashboard"))

        client_id = os.environ.get("MAILCHIMP_CLIENT_ID")
        client_secret = os.environ.get("MAILCHIMP_CLIENT_SECRET")
        redirect_uri = os.environ.get("MAILCHIMP_REDIRECT_URI") or url_for("connect_mailchimp", _external=True)
        if not client_id or not client_secret:
            flash("Set MAILCHIMP_CLIENT_ID and MAILCHIMP_CLIENT_SECRET.", "error")
            return redirect(url_for("dashboard"))

        try:
            token_data = exchange_mailchimp_code(client_id, client_secret, code, redirect_uri)
            server_prefix = token_data.get("dc") or token_data.get("server_prefix")
            if not server_prefix and token_data.get("access_token"):
                metadata = fetch_mailchimp_metadata(token_data.get("access_token"))
                api_endpoint = metadata.get("api_endpoint", "")
                if api_endpoint:
                    server_prefix = api_endpoint.split("//")[-1].split(".")[0]
            if not server_prefix:
                raise ValueError("Mailchimp response missing server prefix")
            # Store token and server_prefix in session to use in audience picker
            session["mailchimp_token"] = token_data.get("access_token")
            session["mailchimp_server_prefix"] = server_prefix
            return redirect(url_for("select_mailchimp_audience"))
        except Exception as error:
            flash(f"Mailchimp connection failed: {error}", "error")
            return redirect(url_for("dashboard"))

    client_id = os.environ.get("MAILCHIMP_CLIENT_ID")
    client_secret = os.environ.get("MAILCHIMP_CLIENT_SECRET")
    if not client_id or not client_secret:
        flash("Set MAILCHIMP_CLIENT_ID and MAILCHIMP_CLIENT_SECRET before connecting Mailchimp.", "error")
        return redirect(url_for("dashboard"))

    state_token = secrets.token_urlsafe(24)
    session["mailchimp_state"] = state_token
    redirect_uri = os.environ.get("MAILCHIMP_REDIRECT_URI") or url_for("connect_mailchimp", _external=True)
    authorize_url = build_mailchimp_authorize_url(client_id, redirect_uri, state_token)
    return redirect(authorize_url)


@app.route("/select_mailchimp_audience", methods=["GET", "POST"])
@login_required
def select_mailchimp_audience():
    user = current_user()
    access_token = session.get("mailchimp_token")
    server_prefix = session.get("mailchimp_server_prefix")
    
    if not access_token or not server_prefix:
        flash("Mailchimp session expired. Please try connecting again.", "error")
        return redirect(url_for("dashboard"))
    
    if request.method == "POST":
        list_id = request.form.get("audience_id", "").strip()
        if not list_id:
            flash("Please select an audience.", "error")
        else:
            try:
                extra_data = {
                    "server_prefix": server_prefix,
                    "list_id": list_id,
                }
                upsert_integration(user["id"], "mailchimp", access_token=access_token, extra_data=extra_data)
                # Mark initial sync so new contacts start as active
                inserted, updated = sync_platform(user["id"], "mailchimp", initial_sync=True)
                flash(f"Mailchimp connected. Synced {inserted + updated} subscribers.", "success")
                # Clean up session
                session.pop("mailchimp_token", None)
                session.pop("mailchimp_server_prefix", None)
                return redirect(url_for("dashboard"))
            except Exception as error:
                flash(f"Failed to sync audience: {error}", "error")
        return render_template(
            "select_mailchimp_audience.html",
            audiences=[],
            selected_audience=list_id,
        )
    
    # Fetch audiences to display
    try:
        audiences = fetch_mailchimp_lists(access_token, server_prefix)
    except Exception as error:
        flash(f"Failed to load audiences: {error}", "error")
        session.pop("mailchimp_token", None)
        session.pop("mailchimp_server_prefix", None)
        return redirect(url_for("dashboard"))
    
    return render_template(
        "select_mailchimp_audience.html",
        audiences=audiences,
    )


@app.route("/connect/convertkit", methods=["GET", "POST"])
@login_required
def connect_convertkit():
    code = request.args.get("code")
    state = request.args.get("state")

    if code:
        expected_state = session.pop("convertkit_state", None)
        if not expected_state or state != expected_state:
            flash("ConvertKit connection failed. Please try again.", "error")
            return redirect(url_for("dashboard"))

        client_id = os.environ.get("CONVERTKIT_CLIENT_ID")
        client_secret = os.environ.get("CONVERTKIT_CLIENT_SECRET")
        redirect_uri = os.environ.get("CONVERTKIT_REDIRECT_URI") or url_for("connect_convertkit", _external=True)
        if not client_id or not client_secret:
            flash("Set CONVERTKIT_CLIENT_ID and CONVERTKIT_CLIENT_SECRET.", "error")
            return redirect(url_for("dashboard"))

        try:
            token_data = exchange_convertkit_code(client_id, client_secret, code, redirect_uri)
            access_token = token_data.get("access_token")
            if not access_token:
                raise ValueError("ConvertKit OAuth token missing access_token")
            session["convertkit_token"] = access_token
            session["convertkit_refresh_token"] = token_data.get("refresh_token")
            return redirect(url_for("select_convertkit_form"))
        except Exception as error:
            flash(f"ConvertKit connection failed: {error}", "error")
            return redirect(url_for("dashboard"))

    client_id = os.environ.get("CONVERTKIT_CLIENT_ID")
    client_secret = os.environ.get("CONVERTKIT_CLIENT_SECRET")
    if not client_id or not client_secret:
        flash("Set CONVERTKIT_CLIENT_ID and CONVERTKIT_CLIENT_SECRET before connecting ConvertKit.", "error")
        return redirect(url_for("dashboard"))

    state_token = secrets.token_urlsafe(24)
    session["convertkit_state"] = state_token
    redirect_uri = os.environ.get("CONVERTKIT_REDIRECT_URI") or url_for("connect_convertkit", _external=True)
    scope = (os.environ.get("CONVERTKIT_SCOPE") or "").strip()
    authorize_url = build_convertkit_authorize_url(client_id, redirect_uri, state_token, scope)
    return redirect(authorize_url)


@app.route("/select_convertkit_form", methods=["GET", "POST"])
@login_required
def select_convertkit_form():
    user = current_user()
    access_token = session.get("convertkit_token")

    if not access_token:
        flash("ConvertKit session expired. Please connect again.", "error")
        return redirect(url_for("dashboard"))

    try:
        forms = fetch_convertkit_forms(access_token=access_token)
    except Exception as error:
        session.pop("convertkit_token", None)
        session.pop("convertkit_refresh_token", None)
        flash(f"Failed to load ConvertKit forms: {error}", "error")
        return redirect(url_for("dashboard"))

    if request.method == "POST":
        form_id = request.form.get("form_id", "").strip()
        if not form_id:
            flash("Please select a ConvertKit form.", "error")
        else:
            try:
                extra_data = {
                    "form_id": form_id,
                    "refresh_token": session.get("convertkit_refresh_token"),
                }
                upsert_integration(user["id"], "convertkit", access_token=access_token, api_key=None, extra_data=extra_data)
                inserted, updated = sync_platform(user["id"], "convertkit", initial_sync=True)
                flash(f"ConvertKit connected. Synced {inserted + updated} subscribers.", "success")
                session.pop("convertkit_token", None)
                session.pop("convertkit_refresh_token", None)
                return redirect(url_for("dashboard"))
            except Exception as error:
                flash(f"Failed to sync ConvertKit form: {error}", "error")

    return render_template(
        "select_source.html",
        title="Select ConvertKit Form",
        description="Choose the form whose subscribers should sync into CullList.",
        source_label="ConvertKit",
        source_type_label="form",
        options=[
            {
                "id": form.get("id"),
                "name": form.get("name") or f"Form {form.get('id')}",
                "detail": form.get("type") or ("archived" if form.get("archived") else "active"),
            }
            for form in forms
        ],
        option_value_key="id",
        option_label_key="name",
        option_detail_key="detail",
        empty_message="No ConvertKit forms were found in this account.",
        submit_label="Sync This Form",
    )


@app.route("/connect/beehiiv", methods=["GET", "POST"])
@login_required
def connect_beehiiv():
    code = request.args.get("code")
    state = request.args.get("state")

    if code:
        expected_state = session.pop("beehiiv_state", None)
        if not expected_state or state != expected_state:
            flash("Beehiiv connection failed. Please try again.", "error")
            return redirect(url_for("dashboard"))

        client_id = os.environ.get("BEEHIIV_CLIENT_ID")
        client_secret = os.environ.get("BEEHIIV_CLIENT_SECRET")
        redirect_uri = os.environ.get("BEEHIIV_REDIRECT_URI") or url_for("connect_beehiiv", _external=True)
        if not client_id or not client_secret:
            flash("Set BEEHIIV_CLIENT_ID and BEEHIIV_CLIENT_SECRET.", "error")
            return redirect(url_for("dashboard"))

        try:
            token_data = exchange_beehiiv_code(client_id, client_secret, code, redirect_uri)
            access_token = token_data.get("access_token")
            if not access_token:
                raise ValueError("Beehiiv OAuth token missing access_token")
            session["beehiiv_token"] = access_token
            session["beehiiv_refresh_token"] = token_data.get("refresh_token")
            return redirect(url_for("select_beehiiv_publication"))
        except Exception as error:
            flash(f"Beehiiv connection failed: {error}", "error")
            return redirect(url_for("dashboard"))

    client_id = os.environ.get("BEEHIIV_CLIENT_ID")
    client_secret = os.environ.get("BEEHIIV_CLIENT_SECRET")
    if not client_id or not client_secret:
        flash("Set BEEHIIV_CLIENT_ID and BEEHIIV_CLIENT_SECRET before connecting Beehiiv.", "error")
        return redirect(url_for("dashboard"))

    state_token = secrets.token_urlsafe(24)
    session["beehiiv_state"] = state_token
    redirect_uri = os.environ.get("BEEHIIV_REDIRECT_URI") or url_for("connect_beehiiv", _external=True)
    scope = os.environ.get("BEEHIIV_SCOPE", "publications:read subscriptions:read")
    authorize_url = build_beehiiv_authorize_url(client_id, redirect_uri, state_token, scope)
    return redirect(authorize_url)


@app.route("/select_beehiiv_publication", methods=["GET", "POST"])
@login_required
def select_beehiiv_publication():
    user = current_user()
    access_token = session.get("beehiiv_token")

    if not access_token:
        flash("Beehiiv session expired. Please connect again.", "error")
        return redirect(url_for("dashboard"))

    try:
        publications = fetch_beehiiv_publications(access_token)
    except Exception:
        publications = []

    fallback_publication_id = os.environ.get("BEEHIIV_PUBLICATION_ID")
    if not publications and fallback_publication_id:
        publications = [{"id": fallback_publication_id, "name": fallback_publication_id}]

    if request.method == "POST":
        publication_id = request.form.get("publication_id", "").strip() or fallback_publication_id
        if not publication_id:
            flash("Please select a Beehiiv publication.", "error")
        else:
            try:
                extra_data = {
                    "publication_id": publication_id,
                    "refresh_token": session.get("beehiiv_refresh_token"),
                }
                upsert_integration(user["id"], "beehiiv", access_token=access_token, api_key=None, extra_data=extra_data)
                inserted, updated = sync_platform(user["id"], "beehiiv", initial_sync=True)
                flash(f"Beehiiv connected. Synced {inserted + updated} subscribers.", "success")
                session.pop("beehiiv_token", None)
                session.pop("beehiiv_refresh_token", None)
                return redirect(url_for("dashboard"))
            except Exception as error:
                flash(f"Failed to sync Beehiiv publication: {error}", "error")

    return render_template(
        "select_source.html",
        title="Select Beehiiv Publication",
        description="Choose the publication whose subscribers should sync into CullList.",
        source_label="Beehiiv",
        source_type_label="publication",
        options=[
            {
                "id": publication.get("id"),
                "name": publication.get("name") or publication.get("title") or f"Publication {publication.get('id')}",
                "detail": publication.get("id"),
            }
            for publication in publications
        ],
        option_value_key="id",
        option_label_key="name",
        option_detail_key="detail",
        empty_message="No Beehiiv publications were found in this account.",
        submit_label="Sync This Publication",
    )


@app.route("/sync/all", methods=["POST"])
@login_required
def sync_all_integrations():
    totals = sync_all_platforms(session["user_id"])
    if not totals:
        flash("No platforms connected yet.", "error")
    else:
        synced_total = sum(item["inserted"] + item["updated"] for item in totals.values())
        flash(f"Synced {synced_total} subscribers from connected platforms.", "success")
    return redirect(url_for("dashboard"))


@app.route("/integrations/<platform>/sync", methods=["POST"])
@login_required
def sync_integration(platform):
    if platform not in PLATFORM_CONFIG:
        abort(404)
    try:
        inserted, updated = sync_platform(session["user_id"], platform)
        flash(f"{PLATFORM_CONFIG[platform]['label']} synced {inserted + updated} subscribers.", "success")
    except Exception as error:
        flash(f"Sync failed for {PLATFORM_CONFIG[platform]['label']}: {error}", "error")
    return redirect(url_for("dashboard"))


@app.route("/integrations/<platform>/preview-delete")
@login_required
def preview_platform_delete(platform):
    if platform not in PLATFORM_CONFIG:
        abort(404)
    count = get_platform_cleanup_count(session["user_id"], platform)
    return render_template(
        "preview_cleanup.html",
        platform=platform,
        platform_label=PLATFORM_CONFIG[platform]["label"],
        count=count,
    )


@app.route("/integrations/<platform>/delete", methods=["POST"])
@login_required
def delete_platform_subscribers_route(platform):
    if platform not in PLATFORM_CONFIG:
        abort(404)
    try:
        removed = cleanup_platform_subscribers(session["user_id"], platform)
        db = get_db()
        row = db.execute(
            "SELECT COUNT(*) AS count FROM subscribers WHERE user_id = ? AND status = 'active'",
            (session["user_id"],),
        ).fetchone()
        engaged = row["count"] or 0
        flash(f"Removed {removed} inactive subscribers from {PLATFORM_CONFIG[platform]['label']}.", "success")
        return redirect(url_for("dashboard", cleaned=removed, engaged=engaged))
    except Exception as error:
        flash(f"Cleanup failed for {PLATFORM_CONFIG[platform]['label']}: {error}", "error")
    return redirect(url_for("dashboard"))


@app.route("/integrations/<platform>/disconnect")
@login_required
def preview_disconnect_integration(platform):
    if platform not in PLATFORM_CONFIG:
        abort(404)

    integration = get_integration(session["user_id"], platform)
    if not integration:
        flash(f"{PLATFORM_CONFIG[platform]['label']} is not connected.", "error")
        return redirect(url_for("dashboard"))

    db = get_db()
    count_row = db.execute(
        """
        SELECT COUNT(*) AS count
        FROM subscribers
        WHERE user_id = ? AND source_platform = ?
        """,
        (session["user_id"], platform),
    ).fetchone()
    synced_count = count_row["count"] or 0

    return render_template(
        "preview_disconnect.html",
        platform=platform,
        platform_label=PLATFORM_CONFIG[platform]["label"],
        synced_count=synced_count,
    )


@app.route("/integrations/<platform>/disconnect", methods=["POST"])
@login_required
def disconnect_integration(platform):
    if platform not in PLATFORM_CONFIG:
        abort(404)

    integration = get_integration(session["user_id"], platform)
    if not integration:
        flash(f"{PLATFORM_CONFIG[platform]['label']} is not connected.", "error")
        return redirect(url_for("dashboard"))

    try:
        remove_integration(session["user_id"], platform)
        flash(f"Disconnected {PLATFORM_CONFIG[platform]['label']} successfully.", "success")
    except Exception as error:
        flash(f"Failed to disconnect {PLATFORM_CONFIG[platform]['label']}: {error}", "error")

    return redirect(url_for("dashboard"))


@app.route("/review-clean/<int:pending_id>")
@login_required
def review_clean(pending_id):
    pending = get_pending_clean(session["user_id"], pending_id)
    if not pending:
        abort(404)

    sample_emails = [item.get("email") for item in pending.get("data_snapshot", [])[:10] if item.get("email")]
    return render_template(
        "review_clean.html",
        pending=pending,
        sample_emails=sample_emails,
    )


@app.route("/review-clean/<int:pending_id>/approve", methods=["POST"])
@login_required
def approve_clean(pending_id):
    pending = get_pending_clean(session["user_id"], pending_id)
    if not pending:
        abort(404)
    if pending.get("status") != "pending":
        flash("This cleanup request has already been processed.", "error")
        return redirect(url_for("dashboard"))

    removed = 0
    failed = 0
    for item in pending.get("data_snapshot", []):
        try:
            if delete_snapshot_item(session["user_id"], item):
                removed += 1
            else:
                failed += 1
        except Exception:
            failed += 1

    update_pending_status(pending_id, session["user_id"], "approved")
    settings = get_auto_clean_settings(session["user_id"])
    upsert_auto_clean_settings(
        session["user_id"],
        enabled=int(settings.get("enabled") or 0),
        frequency=settings.get("frequency", "weekly"),
        pending_review=0,
    )

    if failed:
        flash(f"{removed} inactive subscribers removed successfully. {failed} could not be removed.", "success")
    else:
        flash(f"{removed} inactive subscribers removed successfully.", "success")
    return redirect(url_for("dashboard"))


@app.route("/review-clean/<int:pending_id>/reject", methods=["POST"])
@login_required
def reject_clean(pending_id):
    pending = get_pending_clean(session["user_id"], pending_id)
    if not pending:
        abort(404)

    update_pending_status(pending_id, session["user_id"], "rejected")
    settings = get_auto_clean_settings(session["user_id"])
    upsert_auto_clean_settings(
        session["user_id"],
        enabled=int(settings.get("enabled") or 0),
        frequency=settings.get("frequency", "weekly"),
        pending_review=0,
    )
    flash("Auto-clean request canceled.", "success")
    return redirect(url_for("dashboard"))


@app.route("/jobs/auto-clean", methods=["POST"])
def auto_clean_job_route():
    token = request.headers.get("X-Job-Token") or request.args.get("token")
    expected = os.environ.get("AUTO_CLEAN_JOB_TOKEN")
    if expected and token != expected:
        return jsonify({"error": "unauthorized"}), 401

    result = run_auto_clean_job()
    return jsonify({"status": "ok", **result})


@app.route("/subscribers")
@login_required
def subscribers():
    user = current_user()
    filter_status = request.args.get("status", "all")

    db = get_db()
    if filter_status in {"active", "inactive", "removed"}:
        rows = db.execute(
            "SELECT * FROM subscribers WHERE user_id = ? AND status = ? ORDER BY created_at DESC",
            (user["id"], filter_status),
        ).fetchall()
    else:
        rows = db.execute(
            "SELECT * FROM subscribers WHERE user_id = ? ORDER BY created_at DESC",
            (user["id"],),
        ).fetchall()

    return render_template("subscribers.html", subscribers=rows, filter_status=filter_status)


@app.route("/upload", methods=["GET", "POST"])
@login_required
def upload_csv():
    if request.method == "POST":
        file = request.files.get("csv_file")
        if not file or not file.filename.lower().endswith(".csv"):
            flash("Please upload a valid .csv file.", "error")
            return render_template("upload.html")

        content = file.read().decode("utf-8", errors="ignore")
        reader = csv.reader(io.StringIO(content))

        db = get_db()
        inserted = 0
        seen = set()

        for row in reader:
            if not row:
                continue
            email = row[0].strip().lower()
            if email == "email":
                continue
            if not is_valid_email(email) or email in seen:
                continue

            seen.add(email)
            exists = db.execute(
                "SELECT id FROM subscribers WHERE user_id = ? AND email = ?",
                (session["user_id"], email),
            ).fetchone()
            if exists:
                continue

            db.execute(
                """
                INSERT INTO subscribers (user_id, email, status, created_at)
                VALUES (?, ?, 'active', ?)
                """,
                (session["user_id"], email, now_iso()),
            )
            inserted += 1

        db.commit()
        flash(f"Upload complete. Added {inserted} subscribers.", "success")
        return redirect(url_for("subscribers"))

    return render_template("upload.html")


@app.route("/compose", methods=["GET", "POST"])
@login_required
def compose_email():
    db = get_db()
    user_id = session["user_id"]
    subscribers = db.execute(
        "SELECT id, email, status FROM subscribers WHERE user_id = ? ORDER BY created_at DESC",
        (user_id,),
    ).fetchall()

    generated_email = None
    selected_subscriber = None

    if request.method == "POST":
        subject = request.form.get("subject", "").strip()
        body = request.form.get("body", "").strip()
        subscriber_id = request.form.get("subscriber_id", "").strip()

        if not subject or not body:
            flash("Subject and HTML body are required.", "error")
            return render_template(
                "compose.html",
                subscribers=subscribers,
                generated_email=generated_email,
                selected_subscriber=selected_subscriber,
            )

        target = db.execute(
            "SELECT id, email FROM subscribers WHERE id = ? AND user_id = ?",
            (subscriber_id, user_id),
        ).fetchone()

        if not target:
            flash("Select a valid subscriber for tracking link generation.", "error")
            return render_template(
                "compose.html",
                subscribers=subscribers,
                generated_email=generated_email,
                selected_subscriber=selected_subscriber,
            )

        processed = rewrite_links_with_tracking(body, target["id"])
        processed = inject_tracking_pixel(processed, target["id"])

        db.execute(
            "INSERT INTO emails (user_id, subject, body, created_at) VALUES (?, ?, ?, ?)",
            (user_id, subject, processed, now_iso()),
        )
        db.commit()

        generated_email = {
            "subject": subject,
            "body": processed,
            "subscriber_email": target["email"],
        }
        selected_subscriber = target["id"]
        flash("Email generated with tracking links and saved.", "success")

    return render_template(
        "compose.html",
        subscribers=subscribers,
        generated_email=generated_email,
        selected_subscriber=selected_subscriber,
    )


@app.route("/clean-list", methods=["POST"])
@login_required
def clean_list():
    apply_engagement_rules(session["user_id"])
    flash("Engagement rules applied. Subscriber statuses updated.", "success")
    return redirect(url_for("dashboard"))


@app.route("/warn-inactive", methods=["POST"])
@login_required
def warn_inactive():
    db = get_db()
    rows = db.execute(
        "SELECT email FROM subscribers WHERE user_id = ? AND status = 'inactive'",
        (session["user_id"],),
    ).fetchall()

    for row in rows:
        print(f"[SIMULATED WARNING EMAIL] Sent reminder to inactive subscriber: {row['email']}")

    flash(f"Simulated warning emails sent to {len(rows)} inactive subscribers.", "success")
    return redirect(url_for("dashboard"))


@app.route("/track/open/<int:subscriber_id>")
def track_open(subscriber_id):
    db = get_db()
    db.execute(
        "UPDATE subscribers SET last_opened_at = ? WHERE id = ?",
        (now_iso(), subscriber_id),
    )
    db.commit()

    pixel = io.BytesIO(
        b"\x47\x49\x46\x38\x39\x61\x01\x00\x01\x00\x80\x00\x00\x00\x00\x00\xff\xff\xff\x21\xf9\x04\x01\x00\x00\x00\x00\x2c\x00\x00\x00\x00\x01\x00\x01\x00\x00\x02\x02\x44\x01\x00\x3b"
    )
    return send_file(pixel, mimetype="image/gif")


@app.route("/track/click/<int:subscriber_id>")
def track_click(subscriber_id):
    encoded_url = request.args.get("url", "")
    destination = unquote(encoded_url)

    if not is_safe_redirect_url(destination):
        abort(400, description="Invalid redirect URL")

    db = get_db()
    db.execute(
        "UPDATE subscribers SET last_clicked_at = ? WHERE id = ?",
        (now_iso(), subscriber_id),
    )
    db.commit()

    return redirect(destination)


if __name__ == "__main__":
    import os
    with app.app_context():
        init_db()
    port = int(os.environ.get("PORT", 5000))
    app.run(host="0.0.0.0", port=port, debug=False)
