import sqlite3
import os
from typing import Optional, List, Dict, Any, Tuple

# ponytail: SQLite file-based DB.
# Ceiling: ~500k writes/sec or multi-node clustering.
# Upgrade path: PostgreSQL + asyncpg if sharded across multiple bot processes.

# ponytail: Plaintext token storage in local SQLite.
# Ceiling: Local machine DB compromise.
# Upgrade path: Symmetric encryption (AES-GCM/Fernet) with master key from KMS/env.

DEFAULT_DB_PATH = os.getenv("DATABASE_PATH", "canvas_bot.db")


class Database:
    def __init__(self, db_path: str = DEFAULT_DB_PATH):
        self.db_path = db_path
        self._init_db()

    def _get_connection(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.db_path, check_same_thread=False)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA foreign_keys = ON")
        conn.execute("PRAGMA journal_mode = WAL")
        conn.execute("PRAGMA synchronous = NORMAL")
        return conn

    def _init_db(self):
        with self._get_connection() as conn:
            cursor = conn.cursor()
            cursor.executescript("""
                CREATE TABLE IF NOT EXISTS users (
                    discord_id INTEGER PRIMARY KEY,
                    canvas_token TEXT NOT NULL,
                    canvas_url TEXT NOT NULL DEFAULT 'https://tip.instructure.com',
                    canvas_user_id INTEGER,
                    canvas_name TEXT,
                    is_active INTEGER DEFAULT 1,
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                );

                CREATE TABLE IF NOT EXISTS user_enrollments (
                    discord_id INTEGER NOT NULL,
                    course_id INTEGER NOT NULL,
                    section_id INTEGER NOT NULL,
                    course_name TEXT,
                    section_name TEXT,
                    PRIMARY KEY (discord_id, course_id, section_id),
                    FOREIGN KEY (discord_id) REFERENCES users(discord_id) ON DELETE CASCADE
                );

                CREATE TABLE IF NOT EXISTS server_configs (
                    guild_id INTEGER PRIMARY KEY,
                    section_id INTEGER NOT NULL,
                    course_id INTEGER,
                    course_name TEXT,
                    section_name TEXT,
                    announcements_channel_id INTEGER NOT NULL,
                    assignments_channel_id INTEGER NOT NULL,
                    last_donor_index INTEGER DEFAULT 0,
                    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                );

                CREATE TABLE IF NOT EXISTS seen_announcements (
                    announcement_id TEXT PRIMARY KEY,
                    course_id INTEGER,
                    title TEXT,
                    posted_at TEXT,
                    first_seen_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                );

                CREATE TABLE IF NOT EXISTS user_announcement_deliveries (
                    discord_id INTEGER NOT NULL,
                    announcement_id TEXT NOT NULL,
                    delivered_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    PRIMARY KEY (discord_id, announcement_id)
                );

                CREATE TABLE IF NOT EXISTS server_announcement_deliveries (
                    guild_id INTEGER NOT NULL,
                    announcement_id TEXT NOT NULL,
                    delivered_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    PRIMARY KEY (guild_id, announcement_id)
                );

                CREATE TABLE IF NOT EXISTS server_assignment_deliveries (
                    guild_id INTEGER NOT NULL,
                    assignment_id TEXT NOT NULL,
                    delivered_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    PRIMARY KEY (guild_id, assignment_id)
                );

                CREATE TABLE IF NOT EXISTS user_deadline_reminders (
                    discord_id INTEGER NOT NULL,
                    assignment_id TEXT NOT NULL,
                    sent_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    PRIMARY KEY (discord_id, assignment_id)
                );

                CREATE INDEX IF NOT EXISTS idx_enrollments_section ON user_enrollments(section_id);
                CREATE INDEX IF NOT EXISTS idx_enrollments_course ON user_enrollments(course_id);
                CREATE INDEX IF NOT EXISTS idx_users_active ON users(is_active);
            """)
            conn.commit()

    # --- User operations ---

    def save_user(
        self,
        discord_id: int,
        canvas_token: str,
        canvas_url: str,
        canvas_user_id: Optional[int],
        canvas_name: Optional[str],
        enrollments: List[Dict[str, Any]],
    ) -> None:
        """Upsert user profile and overwrite their active course/section enrollments."""
        with self._get_connection() as conn:
            cursor = conn.cursor()
            cursor.execute(
                """
                INSERT INTO users (discord_id, canvas_token, canvas_url, canvas_user_id, canvas_name, is_active, updated_at)
                VALUES (?, ?, ?, ?, ?, 1, CURRENT_TIMESTAMP)
                ON CONFLICT(discord_id) DO UPDATE SET
                    canvas_token = excluded.canvas_token,
                    canvas_url = excluded.canvas_url,
                    canvas_user_id = excluded.canvas_user_id,
                    canvas_name = excluded.canvas_name,
                    is_active = 1,
                    updated_at = CURRENT_TIMESTAMP
                """,
                (discord_id, canvas_token, canvas_url, canvas_user_id, canvas_name),
            )
            # Re-sync enrollments
            cursor.execute("DELETE FROM user_enrollments WHERE discord_id = ?", (discord_id,))
            for e in enrollments:
                cursor.execute(
                    """
                    INSERT OR IGNORE INTO user_enrollments
                    (discord_id, course_id, section_id, course_name, section_name)
                    VALUES (?, ?, ?, ?, ?)
                    """,
                    (
                        discord_id,
                        e["course_id"],
                        e["section_id"],
                        e.get("course_name", ""),
                        e.get("section_name", ""),
                    ),
                )
            conn.commit()

    def get_user(self, discord_id: int) -> Optional[Dict[str, Any]]:
        with self._get_connection() as conn:
            row = conn.execute("SELECT * FROM users WHERE discord_id = ?", (discord_id,)).fetchone()
            return dict(row) if row else None

    def delete_user(self, discord_id: int) -> bool:
        with self._get_connection() as conn:
            cursor = conn.cursor()
            cursor.execute("DELETE FROM users WHERE discord_id = ?", (discord_id,))
            conn.commit()
            return cursor.rowcount > 0

    def set_user_active(self, discord_id: int, is_active: bool) -> None:
        with self._get_connection() as conn:
            conn.execute("UPDATE users SET is_active = ? WHERE discord_id = ?", (1 if is_active else 0, discord_id))
            conn.commit()

    def get_user_enrollments(self, discord_id: int) -> List[Dict[str, Any]]:
        with self._get_connection() as conn:
            rows = conn.execute(
                "SELECT * FROM user_enrollments WHERE discord_id = ? ORDER BY course_name",
                (discord_id,),
            ).fetchall()
            return [dict(r) for r in rows]

    def get_users_enrolled_in_course(self, course_id: int, section_id: Optional[int] = None) -> List[int]:
        with self._get_connection() as conn:
            if section_id is not None:
                rows = conn.execute(
                    """
                    SELECT DISTINCT u.discord_id FROM users u
                    JOIN user_enrollments ue ON u.discord_id = ue.discord_id
                    WHERE ue.course_id = ? AND ue.section_id = ? AND u.is_active = 1
                    """,
                    (course_id, section_id),
                ).fetchall()
            else:
                rows = conn.execute(
                    """
                    SELECT DISTINCT u.discord_id FROM users u
                    JOIN user_enrollments ue ON u.discord_id = ue.discord_id
                    WHERE ue.course_id = ? AND u.is_active = 1
                    """,
                    (course_id,),
                ).fetchall()
            return [r["discord_id"] for r in rows]

    # --- Server config operations ---

    def save_server_config(
        self,
        guild_id: int,
        section_id: int,
        course_id: Optional[int],
        course_name: Optional[str],
        section_name: Optional[str],
        announcements_channel_id: int,
        assignments_channel_id: int,
    ) -> None:
        with self._get_connection() as conn:
            conn.execute(
                """
                INSERT INTO server_configs 
                (guild_id, section_id, course_id, course_name, section_name, announcements_channel_id, assignments_channel_id, updated_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, CURRENT_TIMESTAMP)
                ON CONFLICT(guild_id) DO UPDATE SET
                    section_id = excluded.section_id,
                    course_id = excluded.course_id,
                    course_name = excluded.course_name,
                    section_name = excluded.section_name,
                    announcements_channel_id = excluded.announcements_channel_id,
                    assignments_channel_id = excluded.assignments_channel_id,
                    updated_at = CURRENT_TIMESTAMP
                """,
                (
                    guild_id,
                    section_id,
                    course_id,
                    course_name,
                    section_name,
                    announcements_channel_id,
                    assignments_channel_id,
                ),
            )
            conn.commit()

    def get_server_config(self, guild_id: int) -> Optional[Dict[str, Any]]:
        with self._get_connection() as conn:
            row = conn.execute("SELECT * FROM server_configs WHERE guild_id = ?", (guild_id,)).fetchone()
            return dict(row) if row else None

    def get_all_server_configs(self) -> List[Dict[str, Any]]:
        with self._get_connection() as conn:
            rows = conn.execute("SELECT * FROM server_configs").fetchall()
            return [dict(r) for r in rows]

    def delete_server_config(self, guild_id: int) -> bool:
        with self._get_connection() as conn:
            cursor = conn.cursor()
            cursor.execute("DELETE FROM server_configs WHERE guild_id = ?", (guild_id,))
            conn.commit()
            return cursor.rowcount > 0

    # --- Donor rotation ---

    def get_section_donors(self, section_id: int) -> List[Dict[str, Any]]:
        """Fetch all active users enrolled in a given section."""
        with self._get_connection() as conn:
            rows = conn.execute(
                """
                SELECT u.discord_id, u.canvas_token, u.canvas_url, u.canvas_name, ue.course_id, ue.course_name, ue.section_name
                FROM users u
                JOIN user_enrollments ue ON u.discord_id = ue.discord_id
                WHERE ue.section_id = ? AND u.is_active = 1
                ORDER BY u.discord_id
                """,
                (section_id,),
            ).fetchall()
            return [dict(r) for r in rows]

    def get_next_donor_for_server(self, guild_id: int, section_id: int) -> Optional[Dict[str, Any]]:
        """
        Selects a donor account from the section and rotates the index every time.
        Round-robin rotation guarantees load-balancing and donor switching across polls.
        """
        donors = self.get_section_donors(section_id)
        if not donors:
            return None

        with self._get_connection() as conn:
            row = conn.execute(
                "SELECT last_donor_index FROM server_configs WHERE guild_id = ?", (guild_id,)
            ).fetchone()
            current_index = row["last_donor_index"] if row and row["last_donor_index"] is not None else 0
            
            chosen_index = current_index % len(donors)
            donor = donors[chosen_index]
            
            next_index = (chosen_index + 1) % len(donors)
            conn.execute(
                "UPDATE server_configs SET last_donor_index = ? WHERE guild_id = ?",
                (next_index, guild_id),
            )
            conn.commit()
            return donor

    def get_any_active_token_for_course(self, course_id: int) -> Optional[Dict[str, Any]]:
        """Returns any active token belonging to a user enrolled in this course."""
        with self._get_connection() as conn:
            row = conn.execute(
                """
                SELECT u.discord_id, u.canvas_token, u.canvas_url, u.canvas_name, ue.course_id
                FROM users u
                JOIN user_enrollments ue ON u.discord_id = ue.discord_id
                WHERE ue.course_id = ? AND u.is_active = 1
                LIMIT 1
                """,
                (course_id,),
            ).fetchone()
            return dict(row) if row else None

    def get_all_distinct_course_ids(self) -> List[int]:
        with self._get_connection() as conn:
            rows = conn.execute(
                """
                SELECT DISTINCT ue.course_id
                FROM user_enrollments ue
                JOIN users u ON ue.discord_id = u.discord_id
                WHERE u.is_active = 1
                """
            ).fetchall()
            return [r["course_id"] for r in rows]

    # --- Delivery tracking & Deduplication ---

    def is_announcement_delivered_to_user(self, discord_id: int, announcement_id: str) -> bool:
        with self._get_connection() as conn:
            row = conn.execute(
                "SELECT 1 FROM user_announcement_deliveries WHERE discord_id = ? AND announcement_id = ?",
                (discord_id, str(announcement_id)),
            ).fetchone()
            return row is not None

    def record_user_announcement_delivery(self, discord_id: int, announcement_id: str) -> None:
        with self._get_connection() as conn:
            conn.execute(
                "INSERT OR IGNORE INTO user_announcement_deliveries (discord_id, announcement_id) VALUES (?, ?)",
                (discord_id, str(announcement_id)),
            )
            conn.commit()

    def is_announcement_delivered_to_server(self, guild_id: int, announcement_id: str) -> bool:
        with self._get_connection() as conn:
            row = conn.execute(
                "SELECT 1 FROM server_announcement_deliveries WHERE guild_id = ? AND announcement_id = ?",
                (guild_id, str(announcement_id)),
            ).fetchone()
            return row is not None

    def record_server_announcement_delivery(self, guild_id: int, announcement_id: str) -> None:
        with self._get_connection() as conn:
            conn.execute(
                "INSERT OR IGNORE INTO server_announcement_deliveries (guild_id, announcement_id) VALUES (?, ?)",
                (guild_id, str(announcement_id)),
            )
            conn.commit()

    def is_assignment_delivered_to_server(self, guild_id: int, assignment_id: str) -> bool:
        with self._get_connection() as conn:
            row = conn.execute(
                "SELECT 1 FROM server_assignment_deliveries WHERE guild_id = ? AND assignment_id = ?",
                (guild_id, str(assignment_id)),
            ).fetchone()
            return row is not None

    def record_server_assignment_delivery(self, guild_id: int, assignment_id: str) -> None:
        with self._get_connection() as conn:
            conn.execute(
                "INSERT OR IGNORE INTO server_assignment_deliveries (guild_id, assignment_id) VALUES (?, ?)",
                (guild_id, str(assignment_id)),
            )
            conn.commit()

    def is_deadline_reminder_sent(self, discord_id: int, assignment_id: str) -> bool:
        with self._get_connection() as conn:
            row = conn.execute(
                "SELECT 1 FROM user_deadline_reminders WHERE discord_id = ? AND assignment_id = ?",
                (discord_id, str(assignment_id)),
            ).fetchone()
            return row is not None

    def record_deadline_reminder(self, discord_id: int, assignment_id: str) -> None:
        with self._get_connection() as conn:
            conn.execute(
                "INSERT OR IGNORE INTO user_deadline_reminders (discord_id, assignment_id) VALUES (?, ?)",
                (discord_id, str(assignment_id)),
            )
            conn.commit()

    def load_cache_sets(self) -> Tuple[set, set, set, set]:
        """
        Pre-load delivered IDs into memory sets for O(1) in-memory checks during polling loops.
        Returns (user_announcements_set, server_announcements_set, server_assignments_set, user_reminders_set)
        """
        with self._get_connection() as conn:
            user_ann = {
                (r[0], r[1])
                for r in conn.execute("SELECT discord_id, announcement_id FROM user_announcement_deliveries").fetchall()
            }
            server_ann = {
                (r[0], r[1])
                for r in conn.execute("SELECT guild_id, announcement_id FROM server_announcement_deliveries").fetchall()
            }
            server_ass = {
                (r[0], r[1])
                for r in conn.execute("SELECT guild_id, assignment_id FROM server_assignment_deliveries").fetchall()
            }
            user_rem = {
                (r[0], r[1])
                for r in conn.execute("SELECT discord_id, assignment_id FROM user_deadline_reminders").fetchall()
            }
            return user_ann, server_ann, server_ass, user_rem
