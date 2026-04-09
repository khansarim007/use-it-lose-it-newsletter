import sqlite3
from datetime import datetime, timedelta

import bcrypt


DB_PATH = "database.db"


def now_iso(days_ago=0):
    return (datetime.utcnow() - timedelta(days=days_ago)).isoformat()


def main():
    conn = sqlite3.connect(DB_PATH)
    cur = conn.cursor()

    cur.executescript(
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

    demo_email = "demo@useitloseit.local"
    demo_password = "demo123"
    password_hash = bcrypt.hashpw(demo_password.encode("utf-8"), bcrypt.gensalt()).decode("utf-8")

    cur.execute("SELECT id FROM users WHERE email = ?", (demo_email,))
    existing = cur.fetchone()
    if existing:
        user_id = existing[0]
        cur.execute("UPDATE users SET password_hash = ? WHERE id = ?", (password_hash, user_id))
        cur.execute("DELETE FROM subscribers WHERE user_id = ?", (user_id,))
        cur.execute("DELETE FROM emails WHERE user_id = ?", (user_id,))
    else:
        cur.execute("INSERT INTO users (email, password_hash) VALUES (?, ?)", (demo_email, password_hash))
        user_id = cur.lastrowid

    subscribers = [
        ("active.one@example.com", "active", now_iso(1), now_iso(1), now_iso(20)),
        ("active.two@example.com", "active", now_iso(2), now_iso(4), now_iso(19)),
        ("inactive.one@example.com", "inactive", now_iso(10), now_iso(2), now_iso(18)),
        ("inactive.two@example.com", "inactive", None, now_iso(6), now_iso(17)),
        ("removed.one@example.com", "removed", now_iso(12), now_iso(20), now_iso(16)),
        ("removed.two@example.com", "removed", None, None, now_iso(15)),
    ]

    cur.executemany(
        """
        INSERT INTO subscribers (user_id, email, status, last_opened_at, last_clicked_at, created_at)
        VALUES (?, ?, ?, ?, ?, ?)
        """,
        [(user_id, email, status, opened, clicked, created) for email, status, opened, clicked, created in subscribers],
    )

    sample_body = (
        "<p>Hello demo subscriber,</p>"
        "<p>Here is your link: <a href='https://example.com'>Read more</a></p>"
    )
    cur.execute(
        "INSERT INTO emails (user_id, subject, body, created_at) VALUES (?, ?, ?, ?)",
        (user_id, "Demo Newsletter", sample_body, now_iso(0)),
    )

    conn.commit()
    conn.close()

    print("Demo seed complete.")
    print("Login email: demo@useitloseit.local")
    print("Login password: demo123")
    print("Seeded subscribers: 6")


if __name__ == "__main__":
    main()
