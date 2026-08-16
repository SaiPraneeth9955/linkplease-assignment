import sqlite3

DB_NAME = "linkplease.db"


def get_connection():
    # This opens (or creates, if it doesn't exist yet) our database file.
    # check_same_thread=False is needed because FastAPI can handle requests
    # from multiple background tasks, not just one.
    conn = sqlite3.connect(DB_NAME, check_same_thread=False)
    # This makes query results behave like dictionaries (access by column name)
    # instead of plain unlabeled tuples — much easier to read.
    conn.row_factory = sqlite3.Row
    return conn


def init_db():
    conn = get_connection()
    cursor = conn.cursor()

    # Table for storing rules the user creates.
    # rule_id is TEXT and PRIMARY KEY, meaning it's the unique identifier for each row.
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS rules (
            rule_id TEXT PRIMARY KEY,
            keyword TEXT NOT NULL,
            dm_message TEXT NOT NULL
        )
    """)

    # Table for storing every incoming webhook event.
    # event_id is UNIQUE — this is what protects us against the ~8%
    # duplicate redelivery the README warned about. If the same event_id
    # arrives twice, the second INSERT will fail, and we catch that in main.py.
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS events (
            event_id TEXT PRIMARY KEY NOT NULL,
            event_type TEXT NOT NULL,
            comment_id TEXT,
            user_id TEXT,
            username TEXT,
            comment_text TEXT,
            processed INTEGER NOT NULL DEFAULT 0
        )
    """)

    # Table for DMs that are ready to be sent (or already sent).
    # UNIQUE(rule_id, user_id) is what guarantees a user never gets
    # DMed twice for the same rule, no matter how many times they comment.
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS dm_queue (
            job_id TEXT PRIMARY KEY,
            rule_id TEXT NOT NULL,
            user_id TEXT NOT NULL,
            comment_id TEXT,
            message TEXT NOT NULL,
            status TEXT NOT NULL DEFAULT 'pending',
            dm_id TEXT,
            attempts INTEGER NOT NULL DEFAULT 0,
            UNIQUE(rule_id, user_id)
        )
    """)
    # Logs every time we correctly blocked a duplicate DM
    # (same user, same rule, already queued/sent).
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS duplicate_dms (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            rule_id TEXT,
            user_id TEXT,
            event_id TEXT
        )
    """)



    conn.commit()
    conn.close()