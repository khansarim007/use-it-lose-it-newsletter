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
    redirect,
    render_template,
    request,
    send_file,
    session,
    url_for,
)

from integrations.beehiiv import delete_subscriber as delete_beehiiv_subscriber
from integrations.beehiiv import fetch_subscribers as fetch_beehiiv_subscribers
from integrations.common import parse_iso_datetime, safe_json_dumps, safe_json_loads
from integrations.convertkit import delete_subscriber as delete_convertkit_subscriber
from integrations.convertkit import fetch_subscribers as fetch_convertkit_subscribers
from integrations.mailchimp import build_authorize_url as build_mailchimp_authorize_url
from integrations.mailchimp import delete_subscriber as delete_mailchimp_subscriber
from integrations.mailchimp import exchange_code_for_token as exchange_mailchimp_code
from integrations.mailchimp import fetch_subscribers as fetch_mailchimp_subscribers


app = Flask(__name__)
app.config["SECRET_KEY"] = os.environ.get("SECRET_KEY", "dev-secret-change-in-production")
DATABASE = "database.db"


PLATFORM_CONFIG = {
    "mailchimp": {
        "label": "Mailchimp",
        "connect_label": "Connect Mailchimp",
        "description": "Sync subscribers from Mailchimp instead of uploading CSVs.",
    },
    "convertkit": {
        "label": "ConvertKit",
        "connect_label": "Connect ConvertKit",
        "description": "Pull subscribers from ConvertKit with your API key.",
    },
    "beehiiv": {
        "label": "Beehiiv",
        "connect_label": "Connect Beehiiv",
        "description": "Fetch Beehiiv subscribers and clean the inactive ones.",
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
        return fetch_mailchimp_subscribers(integration["access_token"], server_prefix, list_id)

    if platform == "convertkit":
        return fetch_convertkit_subscribers(integration["api_key"])

    if platform == "beehiiv":
        publication_id = integration.get("extra_data", {}).get("publication_id") or os.environ.get("BEEHIIV_PUBLICATION_ID")
        return fetch_beehiiv_subscribers(integration["api_key"], publication_id)

    raise ValueError("Unsupported platform")


def upsert_platform_subscribers(user_id, platform, records):
    db = get_db()
    inserted = 0
    updated = 0

    for record in records:
        email = (record.get("email") or "").strip().lower()
        if not is_valid_email(email):
            continue

        source_ref = record.get("source_ref") or email
        source_data = record.get("source_data") or {}
        status = classify_engagement(record.get("last_open"), None, source_data)
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


def sync_platform(user_id, platform):
    records = fetch_platform_subscribers(user_id, platform)
    return upsert_platform_subscribers(user_id, platform, records)


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
            delete_convertkit_subscriber(integration["api_key"], source_data.get("convertkit_id") or row["source_ref"])
        elif platform == "beehiiv":
            publication_id = integration.get("extra_data", {}).get("publication_id") or os.environ.get("BEEHIIV_PUBLICATION_ID")
            delete_beehiiv_subscriber(integration["api_key"], publication_id, source_data.get("beehiiv_id") or row["source_ref"])
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

        if len(password) < 6:
            flash("Password must be at least 6 characters.", "error")
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
    open_rate = (stats["opened"] / total * 100) if total else 0
    click_rate = (stats["clicked"] / total * 100) if total else 0
    platform_cards = build_platform_cards(user["id"])

    return render_template(
        "dashboard.html",
        user=user,
        stats=stats,
        open_rate=round(open_rate, 2),
        click_rate=round(click_rate, 2),
        platform_cards=platform_cards,
    )


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
            if not server_prefix:
                raise ValueError("Mailchimp response missing server prefix")
            extra_data = {
                "server_prefix": server_prefix,
                "list_id": os.environ.get("MAILCHIMP_LIST_ID"),
            }
            upsert_integration(user["id"], "mailchimp", access_token=token_data.get("access_token"), extra_data=extra_data)
            inserted, updated = sync_platform(user["id"], "mailchimp")
            flash(f"Mailchimp connected. Synced {inserted + updated} subscribers.", "success")
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


@app.route("/connect/convertkit", methods=["GET", "POST"])
@login_required
def connect_convertkit():
    if request.method == "POST":
        api_key = request.form.get("api_key", "").strip()
        if not api_key:
            flash("Enter your ConvertKit API key.", "error")
            return render_template(
                "connect.html",
                platform="convertkit",
                platform_label="ConvertKit",
                title="Connect ConvertKit",
                description="Paste your ConvertKit API key and pull subscribers directly into CullList.",
                field_label="ConvertKit API key",
                placeholder="ck_...",
            )

        upsert_integration(session["user_id"], "convertkit", api_key=api_key, extra_data={})
        try:
            inserted, updated = sync_platform(session["user_id"], "convertkit")
            flash(f"ConvertKit connected. Synced {inserted + updated} subscribers.", "success")
        except Exception as error:
            flash(f"ConvertKit saved, but sync failed: {error}", "error")
        return redirect(url_for("dashboard"))

    return render_template(
        "connect.html",
        platform="convertkit",
        platform_label="ConvertKit",
        title="Connect ConvertKit",
        description="Paste your ConvertKit API key and pull subscribers directly into CullList.",
        field_label="ConvertKit API key",
        placeholder="ck_...",
    )


@app.route("/connect/beehiiv", methods=["GET", "POST"])
@login_required
def connect_beehiiv():
    if request.method == "POST":
        api_key = request.form.get("api_key", "").strip()
        if not api_key:
            flash("Enter your Beehiiv API key.", "error")
            return render_template(
                "connect.html",
                platform="beehiiv",
                platform_label="Beehiiv",
                title="Connect Beehiiv",
                description="Paste your Beehiiv API key and sync subscriber data automatically.",
                field_label="Beehiiv API key",
                placeholder="beehiiv_...",
            )

        upsert_integration(
            session["user_id"],
            "beehiiv",
            api_key=api_key,
            extra_data={"publication_id": os.environ.get("BEEHIIV_PUBLICATION_ID")},
        )
        try:
            inserted, updated = sync_platform(session["user_id"], "beehiiv")
            flash(f"Beehiiv connected. Synced {inserted + updated} subscribers.", "success")
        except Exception as error:
            flash(f"Beehiiv saved, but sync failed: {error}", "error")
        return redirect(url_for("dashboard"))

    return render_template(
        "connect.html",
        platform="beehiiv",
        platform_label="Beehiiv",
        title="Connect Beehiiv",
        description="Paste your Beehiiv API key and sync subscriber data automatically.",
        field_label="Beehiiv API key",
        placeholder="beehiiv_...",
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
        flash(f"Removed {removed} inactive subscribers from {PLATFORM_CONFIG[platform]['label']}.", "success")
    except Exception as error:
        flash(f"Cleanup failed for {PLATFORM_CONFIG[platform]['label']}: {error}", "error")
    return redirect(url_for("dashboard"))


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
