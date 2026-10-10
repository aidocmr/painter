"""!
@file db.py
@brief SQLite persistence and data access layer for the Canvas Discord Bot.
@details Manages schema migrations, user credentials, section configurations,
         donor token rotation, notification deduplication, and gamification economy.
"""

import os
import sqlite3
from typing import Any, Dict, List, Optional, Tuple

# ponytail: SQLite file-based DB.
# Ceiling: ~500k writes/sec or multi-node clustering.
# Upgrade path: PostgreSQL + asyncpg if sharded across multiple bot processes.

# ponytail: Plaintext token storage in local SQLite.
# Ceiling: Local machine DB compromise.
# Upgrade path: Symmetric encryption (AES-GCM/Fernet) with master key from KMS/env.

DEFAULT_DB_PATH = os.getenv("DATABASE_PATH", "canvas_bot.db")


class Database:
    """!
    @brief Encapsulates database operations for users, servers, and gamification.
    """

    def __init__(self, db_path: str = DEFAULT_DB_PATH):
        """!
        @brief Initializes the database connection and runs schema migrations.
        @param db_path Filepath to the SQLite database.
        """
        self.db_path = db_path
        self._conn = sqlite3.connect(self.db_path, check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        self._conn.execute("PRAGMA foreign_keys = ON")
        self._conn.execute("PRAGMA journal_mode = WAL")
        self._conn.execute("PRAGMA synchronous = NORMAL")
        self._init_db()

    def close(self) -> None:
        """!
        @brief Closes the active SQLite connection.
        """
        self._conn.close()

    def _get_connection(self) -> sqlite3.Connection:
        """!
        @brief Internal accessor for the database connection.
        @return Active sqlite3.Connection instance.
        """
        return self._conn

    def _init_db(self) -> None:
        """!
        @brief Creates base tables and performs safe column migrations.
        """
        parent_dir = os.path.dirname(self.db_path)
        if parent_dir:
            os.makedirs(parent_dir, exist_ok=True)
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
                    section_id INTEGER,
                    course_id INTEGER,
                    course_name TEXT,
                    section_name TEXT,
                    announcements_channel_id INTEGER,
                    assignments_channel_id INTEGER,
                    last_donor_index INTEGER DEFAULT 0,
                    command_channel_id INTEGER,
                    school_announcements_channel_id INTEGER,
                    school_announcements_role_id INTEGER,
                    canvas_url TEXT DEFAULT 'https://tip.instructure.com',
                    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                );

                CREATE TABLE IF NOT EXISTS server_sections (
                    guild_id INTEGER NOT NULL,
                    section_id INTEGER NOT NULL,
                    course_id INTEGER,
                    course_name TEXT,
                    section_name TEXT,
                    channel_id INTEGER NOT NULL,
                    role_id INTEGER,
                    last_donor_index INTEGER DEFAULT 0,
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    PRIMARY KEY (guild_id, section_id)
                );

                CREATE TABLE IF NOT EXISTS user_economy (
                    discord_id INTEGER PRIMARY KEY,
                    balance INTEGER DEFAULT 0,
                    total_earned INTEGER DEFAULT 0,
                    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                );

                CREATE TABLE IF NOT EXISTS assignment_submissions (
                    assignment_id TEXT NOT NULL,
                    discord_id INTEGER NOT NULL,
                    course_id INTEGER NOT NULL,
                    submitted_at TEXT NOT NULL,
                    submission_rank INTEGER,
                    points_awarded INTEGER DEFAULT 0,
                    awarded_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    PRIMARY KEY (assignment_id, discord_id)
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
                CREATE INDEX IF NOT EXISTS idx_server_sections_section ON server_sections(section_id);
                CREATE INDEX IF NOT EXISTS idx_economy_balance ON user_economy(balance DESC);
                CREATE INDEX IF NOT EXISTS idx_submissions_assignment ON assignment_submissions(assignment_id);
            """)

            # Safe migrations for existing server_configs tables
            existing_cols = {row["name"] for row in conn.execute("PRAGMA table_info(server_configs)").fetchall()}
            for col_name, col_type in [
                ("command_channel_id", "INTEGER"),
                ("school_announcements_channel_id", "INTEGER"),
                ("school_announcements_role_id", "INTEGER"),
                ("canvas_url", "TEXT DEFAULT 'https://tip.instructure.com'"),
            ]:
                if col_name not in existing_cols:
                    conn.execute(f"ALTER TABLE server_configs ADD COLUMN {col_name} {col_type}")

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
        """!
        @brief Upsert user profile and overwrite active course/section enrollments.
        @param discord_id Discord snowflake ID of the user.
        @param canvas_token API access token for Canvas.
        @param canvas_url Base URL of user's institution Canvas instance.
        @param canvas_user_id Canvas internal user identifier.
        @param canvas_name Student's display name on Canvas.
        @param enrollments List of enrolled course/section dictionaries.
        """
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

    def set_user_active(self, discord_id: int, is_active: bool) -> None:
        """!
        @brief Sets a user's active polling status.
        @param discord_id Discord snowflake ID of the user.
        @param is_active Boolean indicating if user account is active.
        """
        with self._get_connection() as conn:
            conn.execute(
                "UPDATE users SET is_active = ?, updated_at = CURRENT_TIMESTAMP WHERE discord_id = ?",
                (1 if is_active else 0, discord_id),
            )
            conn.commit()

    def get_user(self, discord_id: int) -> Optional[Dict[str, Any]]:
        """!
        @brief Fetches user details by Discord ID.
        @param discord_id Discord snowflake ID.
        @return Dictionary of user fields, or None if not found.
        """
        with self._get_connection() as conn:
            row = conn.execute("SELECT * FROM users WHERE discord_id = ?", (discord_id,)).fetchone()
            return dict(row) if row else None

    def delete_user(self, discord_id: int) -> bool:
        """!
        @brief Completely removes a user profile and enrollments.
        @param discord_id Discord snowflake ID.
        @return True if a user row was deleted, False otherwise.
        """
        with self._get_connection() as conn:
            cursor = conn.cursor()
            cursor.execute("DELETE FROM users WHERE discord_id = ?", (discord_id,))
            conn.commit()
            return cursor.rowcount > 0

    def get_user_enrollments(self, discord_id: int) -> List[Dict[str, Any]]:
        """!
        @brief Retrieves all course/section enrollments for a user.
        @param discord_id Discord snowflake ID.
        @return List of enrollment dicts ordered by course_name.
        """
        with self._get_connection() as conn:
            rows = conn.execute(
                "SELECT * FROM user_enrollments WHERE discord_id = ? ORDER BY course_name",
                (discord_id,),
            ).fetchall()
            return [dict(r) for r in rows]

    def get_users_enrolled_in_course(self, course_id: int, section_id: Optional[int] = None) -> List[int]:
        """!
        @brief Retrieves all active Discord user IDs enrolled in a course (or section).
        @param course_id Canvas Course ID.
        @param section_id Optional Canvas Section ID filter.
        @return List of Discord snowflake IDs.
        """
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
        section_id: Optional[int],
        course_id: Optional[int],
        course_name: Optional[str],
        section_name: Optional[str],
        announcements_channel_id: Optional[int],
        assignments_channel_id: Optional[int],
        command_channel_id: Optional[int] = None,
        school_announcements_channel_id: Optional[int] = None,
        school_announcements_role_id: Optional[int] = None,
        canvas_url: Optional[str] = None,
    ) -> None:
        """!
        @brief Upserts server-wide configuration settings.
        @param guild_id Discord Guild snowflake ID.
        @param section_id Primary section ID (legacy or default).
        @param course_id Course ID.
        @param course_name Display name of course.
        @param section_name Display name of section.
        @param announcements_channel_id Channel ID for announcements.
        @param assignments_channel_id Channel ID for assignments.
        @param command_channel_id Dedicated bot command channel ID.
        @param school_announcements_channel_id Global school announcements channel ID.
        @param school_announcements_role_id Role ID mentioned for school announcements.
        @param canvas_url Default Canvas URL for this guild.
        """
        with self._get_connection() as conn:
            conn.execute(
                """
                INSERT INTO server_configs 
                (guild_id, section_id, course_id, course_name, section_name, announcements_channel_id,
                 assignments_channel_id, command_channel_id, school_announcements_channel_id,
                 school_announcements_role_id, canvas_url, updated_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, COALESCE(?, 'https://tip.instructure.com'), CURRENT_TIMESTAMP)
                ON CONFLICT(guild_id) DO UPDATE SET
                    section_id = COALESCE(excluded.section_id, server_configs.section_id),
                    course_id = COALESCE(excluded.course_id, server_configs.course_id),
                    course_name = COALESCE(excluded.course_name, server_configs.course_name),
                    section_name = COALESCE(excluded.section_name, server_configs.section_name),
                    announcements_channel_id = COALESCE(excluded.announcements_channel_id, server_configs.announcements_channel_id),
                    assignments_channel_id = COALESCE(excluded.assignments_channel_id, server_configs.assignments_channel_id),
                    command_channel_id = COALESCE(excluded.command_channel_id, server_configs.command_channel_id),
                    school_announcements_channel_id = COALESCE(excluded.school_announcements_channel_id, server_configs.school_announcements_channel_id),
                    school_announcements_role_id = COALESCE(excluded.school_announcements_role_id, server_configs.school_announcements_role_id),
                    canvas_url = COALESCE(excluded.canvas_url, server_configs.canvas_url),
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
                    command_channel_id,
                    school_announcements_channel_id,
                    school_announcements_role_id,
                    canvas_url,
                ),
            )
            conn.commit()

    def update_server_settings(
        self,
        guild_id: int,
        command_channel_id: Optional[int] = None,
        school_announcements_channel_id: Optional[int] = None,
        school_announcements_role_id: Optional[int] = None,
        canvas_url: Optional[str] = None,
    ) -> None:
        """!
        @brief Modifies server-wide integration settings (command channel, school channel, URL).
        @param guild_id Discord Guild ID.
        @param command_channel_id Dedicated bot command channel ID.
        @param school_announcements_channel_id School announcements channel ID.
        @param school_announcements_role_id Role ID for school announcements.
        @param canvas_url Canvas instance URL.
        """
        with self._get_connection() as conn:
            conn.execute(
                """
                INSERT INTO server_configs (guild_id, command_channel_id, school_announcements_channel_id, school_announcements_role_id, canvas_url, updated_at)
                VALUES (?, ?, ?, ?, COALESCE(?, 'https://tip.instructure.com'), CURRENT_TIMESTAMP)
                ON CONFLICT(guild_id) DO UPDATE SET
                    command_channel_id = CASE WHEN ? IS NOT NULL THEN ? ELSE server_configs.command_channel_id END,
                    school_announcements_channel_id = CASE WHEN ? IS NOT NULL THEN ? ELSE server_configs.school_announcements_channel_id END,
                    school_announcements_role_id = CASE WHEN ? IS NOT NULL THEN ? ELSE server_configs.school_announcements_role_id END,
                    canvas_url = CASE WHEN ? IS NOT NULL THEN ? ELSE server_configs.canvas_url END,
                    updated_at = CURRENT_TIMESTAMP
                """,
                (
                    guild_id,
                    command_channel_id,
                    school_announcements_channel_id,
                    school_announcements_role_id,
                    canvas_url,
                    command_channel_id, command_channel_id,
                    school_announcements_channel_id, school_announcements_channel_id,
                    school_announcements_role_id, school_announcements_role_id,
                    canvas_url, canvas_url,
                ),
            )
            conn.commit()

    def get_server_config(self, guild_id: int) -> Optional[Dict[str, Any]]:
        """!
        @brief Fetches server configuration for a given guild ID.
        @param guild_id Discord Guild snowflake ID.
        @return Dictionary of server config fields or None if unconfigured.
        """
        with self._get_connection() as conn:
            row = conn.execute("SELECT * FROM server_configs WHERE guild_id = ?", (guild_id,)).fetchone()
            return dict(row) if row else None

    def get_all_server_configs(self) -> List[Dict[str, Any]]:
        """!
        @brief Fetches all server configurations.
        @return List of server configuration dicts.
        """
        with self._get_connection() as conn:
            rows = conn.execute("SELECT * FROM server_configs").fetchall()
            return [dict(r) for r in rows]

    def delete_server_config(self, guild_id: int) -> bool:
        """!
        @brief Removes configuration for a guild.
        @param guild_id Discord Guild snowflake ID.
        @return True if deleted, False otherwise.
        """
        with self._get_connection() as conn:
            cursor = conn.cursor()
            cursor.execute("DELETE FROM server_configs WHERE guild_id = ?", (guild_id,))
            cursor.execute("DELETE FROM server_sections WHERE guild_id = ?", (guild_id,))
            conn.commit()
            return cursor.rowcount > 0

    # --- Multiple Sections Architecture ---

    def save_server_section(
        self,
        guild_id: int,
        section_id: int,
        course_id: Optional[int],
        course_name: Optional[str],
        section_name: Optional[str],
        channel_id: int,
        role_id: Optional[int] = None,
    ) -> None:
        """!
        @brief Binds a Canvas section to a dedicated Discord channel and optional role.
        @param guild_id Discord Guild ID.
        @param section_id Canvas Section ID.
        @param course_id Canvas Course ID.
        @param course_name Display name of the course.
        @param section_name Display name of the section.
        @param channel_id Discord channel ID dedicated to this section.
        @param role_id Optional Discord role ID to ping/restrict visibility.
        """
        with self._get_connection() as conn:
            conn.execute(
                """
                INSERT INTO server_sections 
                (guild_id, section_id, course_id, course_name, section_name, channel_id, role_id)
                VALUES (?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(guild_id, section_id) DO UPDATE SET
                    course_id = excluded.course_id,
                    course_name = excluded.course_name,
                    section_name = excluded.section_name,
                    channel_id = excluded.channel_id,
                    role_id = excluded.role_id
                """,
                (guild_id, section_id, course_id, course_name, section_name, channel_id, role_id),
            )
            conn.commit()

    def get_server_sections(self, guild_id: int) -> List[Dict[str, Any]]:
        """!
        @brief Fetches all configured section bindings for a specific guild.
        @param guild_id Discord Guild ID.
        @return List of section mapping dicts.
        """
        with self._get_connection() as conn:
            rows = conn.execute(
                "SELECT * FROM server_sections WHERE guild_id = ? ORDER BY section_id",
                (guild_id,),
            ).fetchall()
            return [dict(r) for r in rows]

    def get_server_section(self, guild_id: int, section_id: int) -> Optional[Dict[str, Any]]:
        """!
        @brief Fetches section configuration for a specific section within a guild.
        @param guild_id Discord Guild ID.
        @param section_id Canvas Section ID.
        @return Dictionary or None.
        """
        with self._get_connection() as conn:
            row = conn.execute(
                "SELECT * FROM server_sections WHERE guild_id = ? AND section_id = ?",
                (guild_id, section_id),
            ).fetchone()
            return dict(row) if row else None

    def get_all_server_sections(self) -> List[Dict[str, Any]]:
        """!
        @brief Fetches all section bindings across all guilds.
        @return List of section mapping dicts.
        """
        with self._get_connection() as conn:
            rows = conn.execute("SELECT * FROM server_sections").fetchall()
            return [dict(r) for r in rows]

    def delete_server_section(self, guild_id: int, section_id: int) -> bool:
        """!
        @brief Deletes a section channel binding.
        @param guild_id Discord Guild ID.
        @param section_id Canvas Section ID.
        @return True if deleted, False otherwise.
        """
        with self._get_connection() as conn:
            cursor = conn.cursor()
            cursor.execute(
                "DELETE FROM server_sections WHERE guild_id = ? AND section_id = ?",
                (guild_id, section_id),
            )
            conn.commit()
            return cursor.rowcount > 0

    def get_next_donor_for_server_section(self, guild_id: int, section_id: int) -> Optional[Dict[str, Any]]:
        """!
        @brief Round-robin selects and rotates a donor account for a section in server_sections.
        @param guild_id Discord Guild ID.
        @param section_id Canvas Section ID.
        @return Selected donor dictionary or None if no donors are enrolled.
        """
        donors = self.get_section_donors(section_id)
        if not donors:
            return None

        with self._get_connection() as conn:
            row = conn.execute(
                "SELECT last_donor_index FROM server_sections WHERE guild_id = ? AND section_id = ?",
                (guild_id, section_id),
            ).fetchone()
            current_index = row["last_donor_index"] if row and row["last_donor_index"] is not None else 0

            chosen_index = current_index % len(donors)
            donor = donors[chosen_index]

            next_index = (chosen_index + 1) % len(donors)
            conn.execute(
                "UPDATE server_sections SET last_donor_index = ? WHERE guild_id = ? AND section_id = ?",
                (next_index, guild_id, section_id),
            )
            conn.commit()
            return donor

    # --- Donor rotation ---

    def get_section_donors(self, section_id: int) -> List[Dict[str, Any]]:
        """!
        @brief Fetches all active users enrolled in a given section to serve as API donors.
        @param section_id Canvas Section ID.
        @return List of user/enrollment dicts ordered by discord_id.
        """
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
        """!
        @brief Selects a donor account from the section and rotates the index every time.
        @param guild_id Discord Guild snowflake ID.
        @param section_id Canvas Section ID.
        @return Selected donor dictionary or None if none exist.
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
        """!
        @brief Returns any active token belonging to a user enrolled in this course.
        @param course_id Canvas Course ID.
        @return Donor record or None.
        """
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
        """!
        @brief Fetches all distinct active course IDs across all registered users.
        @return List of integer course IDs.
        """
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
        """!
        @brief Checks if announcement was delivered to user.
        """
        with self._get_connection() as conn:
            row = conn.execute(
                "SELECT 1 FROM user_announcement_deliveries WHERE discord_id = ? AND announcement_id = ?",
                (discord_id, str(announcement_id)),
            ).fetchone()
            return row is not None

    def is_announcement_delivered_to_server(self, guild_id: int, announcement_id: str) -> bool:
        """!
        @brief Checks if announcement was delivered to server.
        """
        with self._get_connection() as conn:
            row = conn.execute(
                "SELECT 1 FROM server_announcement_deliveries WHERE guild_id = ? AND announcement_id = ?",
                (guild_id, str(announcement_id)),
            ).fetchone()
            return row is not None

    def is_assignment_delivered_to_server(self, guild_id: int, assignment_id: str) -> bool:
        """!
        @brief Checks if assignment was posted to server.
        """
        with self._get_connection() as conn:
            row = conn.execute(
                "SELECT 1 FROM server_assignment_deliveries WHERE guild_id = ? AND assignment_id = ?",
                (guild_id, str(assignment_id)),
            ).fetchone()
            return row is not None

    def is_deadline_reminder_sent(self, discord_id: int, assignment_id: str) -> bool:
        """!
        @brief Checks if a deadline reminder was sent to user for an assignment.
        """
        with self._get_connection() as conn:
            row = conn.execute(
                "SELECT 1 FROM user_deadline_reminders WHERE discord_id = ? AND assignment_id = ?",
                (discord_id, str(assignment_id)),
            ).fetchone()
            return row is not None

    def record_user_announcement_delivery(self, discord_id: int, announcement_id: str) -> None:
        """!
        @brief Records delivery of an announcement to a user.
        """
        with self._get_connection() as conn:
            conn.execute(
                "INSERT OR IGNORE INTO user_announcement_deliveries (discord_id, announcement_id) VALUES (?, ?)",
                (discord_id, str(announcement_id)),
            )

    def record_server_announcement_delivery(self, guild_id: int, announcement_id: str) -> None:
        """!
        @brief Records delivery of an announcement to a server channel.
        """
        with self._get_connection() as conn:
            conn.execute(
                "INSERT OR IGNORE INTO server_announcement_deliveries (guild_id, announcement_id) VALUES (?, ?)",
                (guild_id, str(announcement_id)),
            )

    def record_server_assignment_delivery(self, guild_id: int, assignment_id: str) -> None:
        """!
        @brief Records posting of an assignment to a server channel.
        """
        with self._get_connection() as conn:
            conn.execute(
                "INSERT OR IGNORE INTO server_assignment_deliveries (guild_id, assignment_id) VALUES (?, ?)",
                (guild_id, str(assignment_id)),
            )

    def record_deadline_reminder(self, discord_id: int, assignment_id: str) -> None:
        """!
        @brief Records dispatch of a deadline reminder.
        """
        with self._get_connection() as conn:
            conn.execute(
                "INSERT OR IGNORE INTO user_deadline_reminders (discord_id, assignment_id) VALUES (?, ?)",
                (discord_id, str(assignment_id)),
            )

    def flush_deliveries(
        self,
        user_announcements: list[tuple[int, str]],
        server_announcements: list[tuple[int, str]],
        server_assignments: list[tuple[int, str]],
        deadline_reminders: list[tuple[int, str]],
    ) -> None:
        """!
        @brief Batch-inserts pending delivery records in a single transaction.
        @param user_announcements List of (discord_id, announcement_id).
        @param server_announcements List of (guild_id, announcement_id).
        @param server_assignments List of (guild_id, assignment_id).
        @param deadline_reminders List of (discord_id, assignment_id).
        """
        with self._get_connection() as conn:
            if user_announcements:
                conn.executemany(
                    "INSERT OR IGNORE INTO user_announcement_deliveries (discord_id, announcement_id) VALUES (?, ?)",
                    user_announcements,
                )
            if server_announcements:
                conn.executemany(
                    "INSERT OR IGNORE INTO server_announcement_deliveries (guild_id, announcement_id) VALUES (?, ?)",
                    server_announcements,
                )
            if server_assignments:
                conn.executemany(
                    "INSERT OR IGNORE INTO server_assignment_deliveries (guild_id, assignment_id) VALUES (?, ?)",
                    server_assignments,
                )
            if deadline_reminders:
                conn.executemany(
                    "INSERT OR IGNORE INTO user_deadline_reminders (discord_id, assignment_id) VALUES (?, ?)",
                    deadline_reminders,
                )
            conn.commit()

    def load_cache_sets(self) -> Tuple[set, set, set, set]:
        """!
        @brief Pre-loads delivered IDs into memory sets for O(1) deduplication during polling.
        @return Tuple of (user_announcements, server_announcements, server_assignments, user_reminders).
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

    # --- Gamification & Economy Operations ---

    def get_user_economy(self, discord_id: int) -> Dict[str, Any]:
        """!
        @brief Retrieves coin economy details for a user.
        @param discord_id Discord snowflake ID.
        @return Dict containing balance and total_earned.
        """
        with self._get_connection() as conn:
            row = conn.execute(
                "SELECT balance, total_earned FROM user_economy WHERE discord_id = ?",
                (discord_id,),
            ).fetchone()
            if row:
                return dict(row)
            return {"discord_id": discord_id, "balance": 0, "total_earned": 0}

    def update_user_coins(self, discord_id: int, delta: int) -> int:
        """!
        @brief Adds or subtracts coins from user's balance.
        @param discord_id Discord snowflake ID.
        @param delta Coin amount (positive to award, negative to deduct).
        @return Updated coin balance.
        """
        with self._get_connection() as conn:
            cursor = conn.cursor()
            cursor.execute(
                """
                INSERT INTO user_economy (discord_id, balance, total_earned, updated_at)
                VALUES (?, MAX(0, ?), MAX(0, ?), CURRENT_TIMESTAMP)
                ON CONFLICT(discord_id) DO UPDATE SET
                    balance = MAX(0, balance + ?),
                    total_earned = CASE WHEN ? > 0 THEN total_earned + ? ELSE total_earned END,
                    updated_at = CURRENT_TIMESTAMP
                """,
                (discord_id, delta, delta if delta > 0 else 0, delta, delta, delta),
            )
            conn.commit()
            row = conn.execute("SELECT balance FROM user_economy WHERE discord_id = ?", (discord_id,)).fetchone()
            return row["balance"] if row else 0

    def get_top_leaderboard(self, limit: int = 10) -> List[Dict[str, Any]]:
        """!
        @brief Fetches top users by coin balance for the public leaderboard.
        @param limit Maximum number of leaderboard entries.
        @return List of user economy records with canvas_name if linked.
        """
        with self._get_connection() as conn:
            rows = conn.execute(
                """
                SELECT e.discord_id, e.balance, e.total_earned, u.canvas_name
                FROM user_economy e
                LEFT JOIN users u ON e.discord_id = u.discord_id
                WHERE e.balance > 0 OR e.total_earned > 0
                ORDER BY e.balance DESC, e.total_earned DESC
                LIMIT ?
                """,
                (limit,),
            ).fetchall()
            return [dict(r) for r in rows]

    def is_submission_recorded(self, assignment_id: str, discord_id: int) -> bool:
        """!
        @brief Checks whether a verified submission has already been awarded.
        @param assignment_id Canvas Assignment ID.
        @param discord_id Discord snowflake ID.
        @return True if recorded, False otherwise.
        """
        with self._get_connection() as conn:
            row = conn.execute(
                "SELECT 1 FROM assignment_submissions WHERE assignment_id = ? AND discord_id = ?",
                (str(assignment_id), discord_id),
            ).fetchone()
            return row is not None

    def get_assignment_submissions_count(self, assignment_id: str) -> int:
        """!
        @brief Counts verified submissions already recorded for an assignment.
        @param assignment_id Canvas Assignment ID.
        @return Number of recorded submissions.
        """
        with self._get_connection() as conn:
            row = conn.execute(
                "SELECT COUNT(*) as cnt FROM assignment_submissions WHERE assignment_id = ?",
                (str(assignment_id),),
            ).fetchone()
            return row["cnt"] if row else 0

    def record_submission_and_award_points(
        self,
        assignment_id: str,
        discord_id: int,
        course_id: int,
        submitted_at: str,
    ) -> Tuple[bool, int, int]:
        """!
        @brief Records verified submission and awards order-based points.
        @details Rank 1: 100 coins, Rank 2: 75 coins, Rank 3: 50 coins, Rank 4+: 25 coins.
        @param assignment_id Canvas Assignment ID.
        @param discord_id Discord snowflake ID.
        @param course_id Canvas Course ID.
        @param submitted_at ISO timestamp of submission on Canvas.
        @return Tuple of (awarded: bool, rank: int, points_awarded: int).
        """
        if self.is_submission_recorded(assignment_id, discord_id):
            return False, 0, 0

        with self._get_connection() as conn:
            count = conn.execute(
                "SELECT COUNT(*) as cnt FROM assignment_submissions WHERE assignment_id = ?",
                (str(assignment_id),),
            ).fetchone()["cnt"]
            rank = count + 1

            points_map = {1: 100, 2: 75, 3: 50}
            points = points_map.get(rank, 25)

            conn.execute(
                """
                INSERT INTO assignment_submissions
                (assignment_id, discord_id, course_id, submitted_at, submission_rank, points_awarded, awarded_at)
                VALUES (?, ?, ?, ?, ?, ?, CURRENT_TIMESTAMP)
                """,
                (str(assignment_id), discord_id, course_id, submitted_at, rank, points),
            )
            conn.execute(
                """
                INSERT INTO user_economy (discord_id, balance, total_earned, updated_at)
                VALUES (?, ?, ?, CURRENT_TIMESTAMP)
                ON CONFLICT(discord_id) DO UPDATE SET
                    balance = balance + ?,
                    total_earned = total_earned + ?,
                    updated_at = CURRENT_TIMESTAMP
                """,
                (discord_id, points, points, points, points),
            )
            conn.commit()
            return True, rank, points
