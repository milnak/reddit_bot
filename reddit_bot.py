"""
reddit_bot - Reddit bot
A Reddit bot that reads reply rules from JSON and responds when a post matches one of those regular expressions.

See https://www.wappkit.com/blog/how-to-build-reddit-bot-python-2025
1. Create a dedicated Reddit account for your bot
2. Navigate to reddit.com/prefs/apps while logged into your bot account.
3. Click "create app" or "create another app"
    Name: Something descriptive like "MySubredditMonitorBot"
    App type: Select "script" (this is for personal use)
    Description: Brief description of what the bot does
    About URL: Can be blank
    Redirect URI: Use http://localhost:8080 (required but not used for script apps)
4. Save your credentials (client ID and client secret)

"""

import json
import logging
import re
import sqlite3
import time
from dataclasses import dataclass
from contextlib import contextmanager
from pathlib import Path
from typing import List, Optional

import praw
from dotenv import load_dotenv
import os

load_dotenv()

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[
        logging.StreamHandler(),
        logging.FileHandler(os.environ["REDDITBOT_LOG_PATH"]),
    ],
)
log = logging.getLogger(__name__)

# ── Config ────────────────────────────────────────────────────────────────────

SUBREDDIT = os.environ["REDDITBOT_SUBREDDIT"]
DB_PATH = Path(os.environ["REDDITBOT_DB_PATH"])
RULES_PATH = Path(os.environ["REDDITBOT_RULES_PATH"])

# How long to sleep between each streaming retry on connection errors (seconds)
RETRY_SLEEP = 60


@dataclass(frozen=True)
class ReplyRule:
    pattern: re.Pattern
    reply: str


# ── Database ──────────────────────────────────────────────────────────────────

# Create the table used to track processed Reddit posts.
def init_db(conn: sqlite3.Connection) -> None:
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS processed_posts (
            post_id   TEXT PRIMARY KEY,
            title     TEXT,
            replied   INTEGER NOT NULL DEFAULT 0,
            seen_at   TEXT NOT NULL DEFAULT (datetime('now'))
        )
        """
    )
    conn.commit()


@contextmanager
# Open the SQLite database and ensure the schema exists.
def get_db():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    try:
        init_db(conn)
        yield conn
    finally:
        conn.close()


# Check whether a Reddit post has already been stored in the database.
def already_processed(conn: sqlite3.Connection, post_id: str) -> bool:
    row = conn.execute(
        "SELECT post_id FROM processed_posts WHERE post_id = ?", (post_id,)
    ).fetchone()
    return row is not None


# Record a Reddit post as processed and note whether the bot replied.
def mark_processed(conn: sqlite3.Connection, post_id: str, title: str, replied: bool) -> None:
    conn.execute(
        """
        INSERT OR IGNORE INTO processed_posts (post_id, title, replied)
        VALUES (?, ?, ?)
        """,
        (post_id, title, int(replied)),
    )
    conn.commit()


# ── Reddit client ─────────────────────────────────────────────────────────────

# Build the authenticated PRAW client used by the bot.
# Important: Never hardcode credentials in production code. Use environment variables:
def build_reddit() -> praw.Reddit:
    reddit = praw.Reddit(
        client_id=os.environ["REDDIT_CLIENT_ID"],
        client_secret=os.environ["REDDIT_CLIENT_SECRET"],
        user_agent=f"{os.environ['REDDIT_USER_AGENT']} by /u/{os.environ['REDDIT_USERNAME']}",
        username=os.environ["REDDIT_USERNAME"],
        password=os.environ["REDDIT_PASSWORD"],
        # PRAW respects Reddit's rate limits automatically when ratelimit_seconds is set.
        ratelimit_seconds=300,
    )

    # Verify credentials
    me = reddit.user.me()
    log.info("Authenticated as u/%s", me.name)
    return reddit


# ── Bot logic ─────────────────────────────────────────────────────────────────

# Load reply rules from reddit_bot.json and compile the configured regular expressions.
def load_reply_rules(path: Path = RULES_PATH) -> List[ReplyRule]:
    with path.open("r", encoding="utf-8") as file:
        data = json.load(file)

    if not isinstance(data, list):
        raise ValueError("reddit_bot.json must contain a list of rule objects")

    rules: List[ReplyRule] = []
    for index, item in enumerate(data):
        if not isinstance(item, dict):
            raise ValueError("Each reddit_bot.json entry must be an object")

        pattern_text = item.get("pattern")
        reply_text = item.get("reply")

        if not isinstance(pattern_text, str) or not pattern_text.strip():
            raise ValueError(f"reddit_bot.json entry {index} is missing a pattern")
        if not isinstance(reply_text, str) or not reply_text.strip():
            raise ValueError(f"reddit_bot.json entry {index} is missing a reply")

        rules.append(
            ReplyRule(
                pattern=re.compile(pattern_text, re.IGNORECASE),
                reply=reply_text,
            )
        )

    if not rules:
        raise ValueError("reddit_bot.json must define at least one reply rule")

    return rules


# Find the first configured reply that matches the submission content.
def matching_reply(
    submission: praw.models.Submission,
    rules: List[ReplyRule],
) -> Optional[ReplyRule]:
    content = f"{submission.title}\n{submission.selftext or ''}"
    for rule in rules:
        if rule.pattern.search(content):
            return rule
    return None


# Handle one submission by replying when it matches and recording the result.
def handle_submission(
    submission: praw.models.Submission,
    conn: sqlite3.Connection,
    rules: List[ReplyRule],
) -> None:
    post_id = submission.id

    if already_processed(conn, post_id):
        log.debug("Skipping already-processed post %s", post_id)
        return

    matched_rule = matching_reply(submission, rules)
    replied = False

    if matched_rule is not None:
        try:
            # Reply to the submission
            submission.reply(matched_rule.reply)

            # Add your own delays for write operations.
            time.sleep(5)

            replied = True
            log.info('Replied to post %s: "%s"', post_id, submission.title[:80],)
        except praw.exceptions.RedditAPIException as exc:
            # PRAW already backs off on rate limits; log other API errors.
            log.warning("API error replying to %s: %s", post_id, exc)
    else:
        log.debug('No trigger found in post %s: "%s"', post_id, submission.title[:80])

    mark_processed(conn, post_id, submission.title, replied)


# Start the streaming loop and recover from transient Reddit or runtime errors.
def run() -> None:
    rules = load_reply_rules()
    reddit = build_reddit()

    # Use r/test for testing
    subreddit = reddit.subreddit(SUBREDDIT)
    log.info(f"Monitoring r/{subreddit.display_name} ...")

    while True:
        try:
            with get_db() as conn:
                # Stream new submissions from the subreddit
                for submission in subreddit.stream.submissions(skip_existing=True):
                    handle_submission(submission, conn, rules)
        except praw.exceptions.PRAWException as exc:
            log.error("PRAW error: %s — retrying in %ds", exc, RETRY_SLEEP)
            time.sleep(RETRY_SLEEP)
        except Exception as exc:  # noqa: BLE001
            log.error("Unexpected error: %s — retrying in %ds", exc, RETRY_SLEEP)
            time.sleep(RETRY_SLEEP)


if __name__ == "__main__":
    run()
