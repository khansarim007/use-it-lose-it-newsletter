import csv
import io
import re
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


app = Flask(__name__)
app.config["SECRET_KEY"] = "dev-secret-change-in-production"
DATABASE = "database.db"


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
        """
    )
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
    db = get_db()
    subs = db.execute(
        "SELECT id, last_opened_at, last_clicked_at FROM subscribers WHERE user_id = ?",
        (user_id,),
    ).fetchall()

    removed_cutoff = datetime.utcnow() - timedelta(days=14)
    inactive_cutoff = datetime.utcnow() - timedelta(days=7)

    for sub in subs:
        last_clicked = datetime.fromisoformat(sub["last_clicked_at"]) if sub["last_clicked_at"] else None
        last_opened = datetime.fromisoformat(sub["last_opened_at"]) if sub["last_opened_at"] else None

        if not last_clicked or last_clicked < removed_cutoff:
            status = "removed"
        elif not last_opened or last_opened < inactive_cutoff:
            status = "inactive"
        else:
            status = "active"

        db.execute("UPDATE subscribers SET status = ? WHERE id = ?", (status, sub["id"]))

    db.commit()


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
    flash("Logged out.", "success")
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

    return render_template(
        "dashboard.html",
        user=user,
        stats=stats,
        open_rate=round(open_rate, 2),
        click_rate=round(click_rate, 2),
    )


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
