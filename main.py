import uuid
import sqlite3
import threading
import time
import os
import requests

from collections import deque
from contextlib import asynccontextmanager
from dotenv import load_dotenv
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel

from database import init_db, get_connection


# ---------------------------------------------------------
# ENVIRONMENT / API CONFIG
# ---------------------------------------------------------

load_dotenv()

PSEUDOGRAM_BASE_URL = "https://pseudogram-api.onrender.com"
API_KEY = os.getenv("PSEUDOGRAM_API_KEY")

init_db()


# ---------------------------------------------------------
# KEYWORD MATCHING
# ---------------------------------------------------------

def keyword_match(comment_text, keyword):
    if not comment_text:
        return False

    return keyword.lower() in comment_text.lower()


# ---------------------------------------------------------
# EVENT PROCESSING WORKER
# ---------------------------------------------------------

def process_events_loop():

    while True:

        conn = get_connection()
        cursor = conn.cursor()

        cursor.execute(
            "SELECT * FROM events WHERE processed = 0"
        )

        pending_events = cursor.fetchall()

        for event in pending_events:

            if event["event_type"] == "comment.created":

                cursor.execute("SELECT * FROM rules")
                rules = cursor.fetchall()

                for rule in rules:

                    if keyword_match(
                        event["comment_text"],
                        rule["keyword"]
                    ):

                        job_id = str(uuid.uuid4())

                        try:

                            cursor.execute(
                                """INSERT INTO dm_queue
                                   (job_id, rule_id, user_id,
                                    comment_id, message)
                                   VALUES (?, ?, ?, ?, ?)""",
                                (
                                    job_id,
                                    rule["rule_id"],
                                    event["user_id"],
                                    event["comment_id"],
                                    rule["dm_message"]
                                )
                            )

                            conn.commit()

                        except sqlite3.IntegrityError:
                            # UNIQUE(rule_id, user_id) prevented a duplicate DM.
                            # Log it so /stats can report an honest count.
                            cursor.execute(
                                """INSERT INTO duplicate_dms (rule_id, user_id, event_id)
                                   VALUES (?, ?, ?)""",
                                (rule["rule_id"], event["user_id"], event["event_id"])
                            )
                            conn.commit()

            # Mark event as processed.
            cursor.execute(
                "UPDATE events SET processed = 1 WHERE event_id = ?",
                (event["event_id"],)
            )

            conn.commit()

        conn.close()

        time.sleep(1)


# ---------------------------------------------------------
# RATE LIMITER
# ---------------------------------------------------------

recent_call_times = deque()

MAX_CALLS_PER_WINDOW = 10
WINDOW_SECONDS = 60


def wait_for_rate_limit_slot():

    while True:

        now = time.monotonic()

        while recent_call_times:

            if now - recent_call_times[0] >= WINDOW_SECONDS:
                recent_call_times.popleft()
            else:
                break

        if len(recent_call_times) < MAX_CALLS_PER_WINDOW:

            recent_call_times.append(now)
            return

        time.sleep(1)


# ---------------------------------------------------------
# DM SENDING
# ---------------------------------------------------------

MAX_ATTEMPTS = 5


def send_dm(job):

    wait_for_rate_limit_slot()

    try:

        response = requests.post(
            f"{PSEUDOGRAM_BASE_URL}/v1/dm/send",

            headers={
                "X-API-Key": API_KEY,
                "Idempotency-Key": job["job_id"]
            },

            json={
                "recipient_user_id": job["user_id"],
                "message": job["message"],
                "comment_id": job["comment_id"]
            },

            timeout=10
        )

    except requests.RequestException:

        return "retry", None

    # Successful response. The assignment documents 202 Accepted,
    # but the mock API may also return 200 with a delivered status.
    if response.status_code in (200, 202):

        try:
            data = response.json()
        except ValueError:
            return "retry", None

        dm_id = data.get("dm_id")
        status = data.get("status")

        # The mock API can report an already-delivered DM.
        if status == "delivered":
            return "delivered", dm_id

        # 202 normally means accepted/queued.
        if dm_id:
            return "accepted", dm_id

        return "retry", None

    # Rate limited.
    if response.status_code == 429:

        retry_after = int(
            response.headers.get("Retry-After", "5")
        )

        time.sleep(retry_after)

        return "retry", None

    # Temporary server error.
    if response.status_code == 500:

        return "retry", None

    # Invalid request.
    if response.status_code == 400:

        return "failed", None

    # Unexpected response.
    return "retry", None


# ---------------------------------------------------------
# CHECK DELIVERY STATUS
# ---------------------------------------------------------

def check_dm_status(dm_id):

    try:

        response = requests.get(
            f"{PSEUDOGRAM_BASE_URL}/v1/dm/{dm_id}",

            headers={
                "X-API-Key": API_KEY
            },

            timeout=10
        )

        if response.status_code == 200:

            return response.json().get("status")

    except requests.RequestException:

        pass

    return None


# ---------------------------------------------------------
# DM WORKER
# ---------------------------------------------------------

def dm_worker_loop():

    while True:

        conn = get_connection()
        cursor = conn.cursor()

        # -------------------------------------------------
        # Send pending DM jobs
        # -------------------------------------------------

        cursor.execute(
            "SELECT * FROM dm_queue WHERE status = 'pending'"
        )

        pending_jobs = cursor.fetchall()

        for job in pending_jobs:

            # Give up after maximum attempts.
            if job["attempts"] >= MAX_ATTEMPTS:

                cursor.execute(
                    """UPDATE dm_queue
                       SET status = 'failed'
                       WHERE job_id = ?""",
                    (job["job_id"],)
                )

                conn.commit()

                continue

            result, dm_id = send_dm(job)

            # The API already confirmed delivery.
            if result == "delivered":

                cursor.execute(
                    """UPDATE dm_queue
                       SET status = 'sent',
                           dm_id = ?,
                           attempts = attempts + 1
                       WHERE job_id = ?""",
                    (
                        dm_id,
                        job["job_id"]
                    )
                )

            # API accepted the DM but delivery still needs confirmation.
            elif result == "accepted":

                cursor.execute(
                    """UPDATE dm_queue
                       SET status = 'queued_api',
                           dm_id = ?,
                           attempts = attempts + 1
                       WHERE job_id = ?""",
                    (
                        dm_id,
                        job["job_id"]
                    )
                )

            # Permanent invalid request.
            elif result == "failed":

                cursor.execute(
                    """UPDATE dm_queue
                       SET status = 'failed',
                           attempts = attempts + 1
                       WHERE job_id = ?""",
                    (job["job_id"],)
                )

            # Temporary failure.
            else:

                cursor.execute(
                    """UPDATE dm_queue
                       SET attempts = attempts + 1
                       WHERE job_id = ?""",
                    (job["job_id"],)
                )

            conn.commit()

        # -------------------------------------------------
        # Check DMs accepted by PseudoGram
        # -------------------------------------------------

        cursor.execute(
            "SELECT * FROM dm_queue WHERE status = 'queued_api'"
        )

        accepted_jobs = cursor.fetchall()

        for job in accepted_jobs:

            status = check_dm_status(job["dm_id"])

            # Successfully delivered.
            if status == "delivered":

                cursor.execute(
                    """UPDATE dm_queue
                       SET status = 'sent'
                       WHERE job_id = ?""",
                    (job["job_id"],)
                )

            # API accepted it but later delivery failed.
            elif status == "failed":

                cursor.execute(
                    """UPDATE dm_queue
                       SET status = 'pending'
                       WHERE job_id = ?""",
                    (job["job_id"],)
                )

            conn.commit()

        conn.close()

        time.sleep(2)


# ---------------------------------------------------------
# FASTAPI LIFESPAN
# ---------------------------------------------------------

@asynccontextmanager
async def lifespan(app: FastAPI):

    threading.Thread(
        target=process_events_loop,
        daemon=True
    ).start()

    threading.Thread(
        target=dm_worker_loop,
        daemon=True
    ).start()

    yield


app = FastAPI(lifespan=lifespan)


# ---------------------------------------------------------
# RULE MODEL
# ---------------------------------------------------------

class RuleRequest(BaseModel):
    keyword: str
    dm_message: str


# ---------------------------------------------------------
# HOME
# ---------------------------------------------------------

@app.get("/")
def read_root():

    return {
        "message": "LinkPlease webhook server is alive"
    }


# ---------------------------------------------------------
# CREATE RULE
# ---------------------------------------------------------

@app.post("/rules", status_code=201)
def create_rule(rule: RuleRequest):

    rule_id = str(uuid.uuid4())

    conn = get_connection()
    cursor = conn.cursor()

    cursor.execute(
        """INSERT INTO rules
           (rule_id, keyword, dm_message)
           VALUES (?, ?, ?)""",
        (
            rule_id,
            rule.keyword,
            rule.dm_message
        )
    )

    conn.commit()
    conn.close()

    return {
        "rule_id": rule_id,
        "keyword": rule.keyword,
        "dm_message": rule.dm_message
    }


# ---------------------------------------------------------
# WEBHOOK
# ---------------------------------------------------------

@app.post("/webhook")
async def receive_webhook(request: Request):

    payload = await request.json()

    event_id = payload.get("event_id")
    event_type = payload.get("event_type")

    data = payload.get("data", {})

    comment_id = data.get("comment_id")
    comment_text = data.get("text")

    from_info = data.get("from", {})

    user_id = from_info.get("user_id")
    username = from_info.get("username")

    conn = get_connection()
    cursor = conn.cursor()

    try:

        cursor.execute(
            """INSERT INTO events
               (event_id, event_type, comment_id,
                user_id, username, comment_text)
               VALUES (?, ?, ?, ?, ?, ?)""",
            (
                event_id,
                event_type,
                comment_id,
                user_id,
                username,
                comment_text
            )
        )

        conn.commit()

    except sqlite3.IntegrityError:

        conn.close()

        return JSONResponse(
            status_code=200,
            content={
                "status": "duplicate ignored"
            }
        )

    conn.close()

    return JSONResponse(
        status_code=200,
        content={
            "status": "received"
        }
    )


# ---------------------------------------------------------
# DEBUG QUEUE
# ---------------------------------------------------------

@app.get("/debug/queue")
def debug_queue():

    conn = get_connection()
    cursor = conn.cursor()

    cursor.execute(
        "SELECT * FROM dm_queue"
    )

    rows = [
        dict(row)
        for row in cursor.fetchall()
    ]

    conn.close()

    return rows


# ---------------------------------------------------------
# STATS
# ---------------------------------------------------------

@app.get("/stats")
def get_stats():

    conn = get_connection()
    cursor = conn.cursor()

    cursor.execute(
        "SELECT COUNT(*) as c FROM dm_queue WHERE status = 'sent'"
    )
    sent = cursor.fetchone()["c"]

    cursor.execute(
        "SELECT COUNT(*) as c FROM dm_queue WHERE status = 'failed'"
    )
    failed = cursor.fetchone()["c"]

    cursor.execute(
        """SELECT COUNT(*) as c
           FROM dm_queue
           WHERE status IN ('pending', 'queued_api')"""
    )
    queued = cursor.fetchone()["c"]

    cursor.execute(
        "SELECT COUNT(*) as c FROM duplicate_dms"
    )
    duplicates_blocked = cursor.fetchone()["c"]

    conn.close()

    return {
        "sent": sent,
        "failed": failed,
        "queued": queued,
        "duplicates_blocked": duplicates_blocked
    }