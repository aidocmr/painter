"""!
@file bot.py
@brief Core Discord bot implementation for Canvas LMS integration.
@details Provides slash commands, channel and role architecture, multi-school support,
         multiple sections with donor credential rotation, verified submission gamification,
         and background polling synchronization.
"""

import asyncio
import os
import random
import warnings
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional, Set, Tuple

import discord
from discord import app_commands
from discord.ext import commands, tasks
from dotenv import load_dotenv

from painterv3.canvas_client import (
    async_check_user_assignment_completion,
    async_fetch_announcements,
    async_fetch_assignments,
    async_fetch_todo_items,
    async_get_user_submission,
    async_validate_and_get_profile,
    parse_canvas_date,
)
from painterv3.db import DEFAULT_DB_PATH, Database

# ponytail: In-memory cache sets loaded at startup for deduplication.
# Ceiling: Memory exhaustion if millions of deliveries over years.
# Upgrade path: LRU cache with Redis or TTL index in DB.

# ponytail: Polling Canvas REST API every 5 minutes.
# Ceiling: Notification delay up to polling interval.
# Upgrade path: Canvas Outgoing Webhooks or Canvas Event Streaming with an HTTP receiver.

load_dotenv()
BOT_TOKEN = os.getenv("BOT_TOKEN") or os.getenv("DISCORD_TOKEN")
DEFAULT_CANVAS_URL = os.getenv("CANVAS_API_URL", "https://tip.instructure.com")

# Optional: Restrict bot to specific server IDs
_raw_guild_ids = os.getenv("ALLOWED_GUILD_IDS", "")
ALLOWED_GUILD_IDS = {int(g.strip()) for g in _raw_guild_ids.split(",") if g.strip().isdigit()}

# Deprecation control: Direct DM broadcasts default to False
ENABLE_DM_BROADCASTS = os.getenv("ENABLE_DM_BROADCASTS", "false").lower() in ("true", "1")


class LoginModal(discord.ui.Modal, title="Canvas Account Login"):
    """!
    @brief Modal dialog for student Canvas API token authentication.
    """

    canvas_token = discord.ui.TextInput(
        label="Canvas API Access Token",
        style=discord.TextStyle.short,
        placeholder="Paste your token from Canvas -> Settings -> New Access Token",
        required=True,
        min_length=10,
        max_length=200,
    )
    canvas_url = discord.ui.TextInput(
        label="Canvas Instance URL",
        style=discord.TextStyle.short,
        default=DEFAULT_CANVAS_URL,
        placeholder="e.g. https://canvas.instructure.com or https://tip.instructure.com",
        required=False,
    )

    def __init__(self, bot_instance: "CanvasDiscordBot", default_url: Optional[str] = None):
        """!
        @brief Initializes modal with reference to bot and default Canvas endpoint.
        @param bot_instance Active bot instance.
        @param default_url Optional server-specific Canvas URL.
        """
        super().__init__()
        self.bot = bot_instance
        if default_url:
            self.canvas_url.default = default_url

    async def on_submit(self, interaction: discord.Interaction):
        """!
        @brief Submits and validates Canvas credentials against the specified institution.
        @param interaction The Discord interaction triggering the submission.
        """
        await interaction.response.defer(ephemeral=True, thinking=True)
        token = str(self.canvas_token.value).strip()
        url = str(self.canvas_url.value).strip() or DEFAULT_CANVAS_URL

        try:
            profile, enrollments = await async_validate_and_get_profile(url, token)
        except Exception as e:
            await interaction.followup.send(
                f"❌ **Canvas Login Failed**: Could not authenticate with Canvas ({e}).\n"
                "Please verify your token and Canvas URL.",
                ephemeral=True,
            )
            return

        # Save to database
        self.bot.db.save_user(
            discord_id=interaction.user.id,
            canvas_token=token,
            canvas_url=url,
            canvas_user_id=profile.get("id"),
            canvas_name=profile.get("name"),
            enrollments=enrollments,
        )

        courses_summary = ""
        seen_courses = set()
        for e in enrollments:
            cid = e["course_id"]
            if cid not in seen_courses:
                seen_courses.add(cid)
                s_name = f" ({e['section_name']})" if e.get("section_name") else ""
                courses_summary += f"• **{e.get('course_name', f'Course {cid}')}**{s_name}\n"

        if not courses_summary:
            courses_summary = "No active course enrollments found."
        elif len(seen_courses) > 10:
            lines = courses_summary.strip().split("\n")
            courses_summary = "\n".join(lines[:10]) + f"\n*...and {len(seen_courses) - 10} more courses.*"

        embed = discord.Embed(
            title="✅ Canvas Connected Successfully!",
            description=(
                f"Welcome, **{profile.get('name')}** (Canvas ID: `{profile.get('id')}`)!\n\n"
                f"Connected to **{url}** with **{len(seen_courses)} active courses**.\n"
                "Updates will be posted to your server section channels. "
                "Check your tasks anytime with `/deadlines`, `/my_assignments`, and `/todo`."
            ),
            color=discord.Color.green(),
        )
        embed.add_field(name="Enrolled Courses", value=courses_summary, inline=False)
        embed.set_footer(text="Your token serves as a shared section donor and verifies your assignment submissions.")

        await interaction.followup.send(embed=embed, ephemeral=True)


class CanvasDiscordBot(commands.Bot):
    """!
    @brief Discord bot handling Canvas commands, gamification, and multi-server synchronization.
    """

    def __init__(
        self,
        db_path: str = DEFAULT_DB_PATH,
        allowed_guild_ids: Optional[Set[int]] = None,
        enable_dm_broadcasts: Optional[bool] = None,
    ):
        """!
        @brief Initializes the bot with database connection and configuration.
        @param db_path Filepath to SQLite database.
        @param allowed_guild_ids Optional set of guild IDs authorized to use the bot.
        @param enable_dm_broadcasts Flag for legacy DM broadcast delivery (deprecated).
        """
        intents = discord.Intents.default()
        super().__init__(command_prefix="!", intents=intents)

        self.db = Database(db_path=db_path)
        self.allowed_guild_ids: Set[int] = (
            allowed_guild_ids if allowed_guild_ids is not None else set(ALLOWED_GUILD_IDS)
        )
        self.enable_dm_broadcasts: bool = (
            enable_dm_broadcasts if enable_dm_broadcasts is not None else ENABLE_DM_BROADCASTS
        )

        self.user_announcements_cache: Set[Tuple[int, str]] = set()
        self.server_announcements_cache: Set[Tuple[int, str]] = set()
        self.server_assignments_cache: Set[Tuple[int, str]] = set()
        self.user_reminders_cache: Set[Tuple[int, str]] = set()
        self.cached_assignments_pool: Dict[int, List[Dict[str, Any]]] = {}

    async def setup_hook(self):
        """!
        @brief Discord setup hook executed before connection.
        @details Pre-loads cache sets, registers slash commands, and starts background loop.
        """
        u_ann, s_ann, s_ass, u_rem = self.db.load_cache_sets()
        self.user_announcements_cache = u_ann
        self.server_announcements_cache = s_ann
        self.server_assignments_cache = s_ass
        self.user_reminders_cache = u_rem
        print(
            f"[Cache] Loaded {len(u_ann)} user deliveries, {len(s_ann)} server announcements, "
            f"{len(s_ass)} server assignments, {len(u_rem)} deadline reminders into memory."
        )

        self._register_commands()
        try:
            print("[Bot] Syncing slash command tree...")
            synced = await asyncio.wait_for(self.tree.sync(), timeout=15.0)
            print(f"[Bot] Synced {len(synced)} slash commands: {[c.name for c in synced]}")
        except asyncio.TimeoutError:
            print("[Bot] Command sync timed out (Discord rate limit). Commands remain registered.")
        except Exception as e:
            print(f"[Bot] Failed to sync slash commands: {e}")

        if not self.scan_canvas_loop.is_running():
            self.scan_canvas_loop.start()

    def _is_guild_authorized(self, guild_id: Optional[int]) -> bool:
        """!
        @brief Validates whether a guild ID is authorized to interact with the bot.
        @param guild_id Discord Guild snowflake ID.
        @return True if authorized or no restrictions configured, False otherwise.
        """
        if not self.allowed_guild_ids or guild_id is None:
            return True
        return guild_id in self.allowed_guild_ids

    def _get_target_command_channel(self, guild: Optional[discord.Guild]) -> Optional[int]:
        """!
        @brief Finds the designated bot command channel (#canvas-bot) for a guild.
        @param guild Discord Guild object.
        @return Channel ID or None.
        """
        if not guild:
            return None
        cfg = self.db.get_server_config(guild.id)
        if cfg and cfg.get("command_channel_id"):
            return cfg["command_channel_id"]
        for ch in guild.text_channels:
            if ch.name.lower() in ("canvas-bot", "bot-commands"):
                return ch.id
        return None

    def _register_commands(self):
        """!
        @brief Registers all slash commands and global interaction checks.
        """
        if getattr(self, "_commands_registered", False):
            return
        self._commands_registered = True

        # Global interaction check for guild restrictions and dedicated command channel
        async def global_interaction_check(interaction: discord.Interaction) -> bool:
            # 1. Guild ID authorization check
            if interaction.guild_id and not self._is_guild_authorized(interaction.guild_id):
                done = interaction.response.is_done() if callable(getattr(interaction.response, "is_done", None)) else False
                if asyncio.iscoroutine(done):
                    done.close()
                    done = False
                if not done:
                    await interaction.response.send_message(
                        "❌ **Access Denied**: This bot is only authorized to operate in designated servers.",
                        ephemeral=True,
                    )
                return False

            # 2. Dedicated Command Channel (#canvas-bot) check
            if interaction.guild and interaction.channel:
                target_ch_id = self._get_target_command_channel(interaction.guild)
                if target_ch_id and interaction.channel_id != target_ch_id:
                    # Administrators and server configuration commands bypass restriction
                    is_admin = False
                    if hasattr(interaction.user, "guild_permissions"):
                        is_admin = interaction.user.guild_permissions.administrator

                    cmd_name = interaction.command.name if interaction.command else ""
                    admin_commands = {
                        "server_setup",
                        "server_status",
                        "server_unlink",
                        "section_setup",
                        "section_list",
                        "section_unlink",
                        "set_command_channel",
                        "set_school_announcements",
                        "set_canvas_url",
                        "sync_now",
                        "sync",
                    }
                    if not is_admin and cmd_name not in admin_commands:
                        done = interaction.response.is_done() if callable(getattr(interaction.response, "is_done", None)) else False
                        if asyncio.iscoroutine(done):
                            done.close()
                            done = False
                        if not done:
                            await interaction.response.send_message(
                                f"⚠️ **Channel Restricted**: Canvas Bot commands must be used in <#{target_ch_id}>.",
                                ephemeral=True,
                            )
                        return False

            return True

        self.tree.interaction_check = global_interaction_check

        # --- Prefix Commands ---
        @self.command(name="sync")
        @commands.is_owner()
        async def sync_prefix(ctx: commands.Context, guild_only: bool = True):
            """!
            @brief Instantly sync slash commands to current server or globally.
            """
            if guild_only and ctx.guild:
                self.tree.copy_global_to(guild=ctx.guild)
                synced = await self.tree.sync(guild=ctx.guild)
                await ctx.reply(f"✅ Synced {len(synced)} slash commands directly to **{ctx.guild.name}**!")
            else:
                synced = await self.tree.sync()
                await ctx.reply(f"✅ Synced {len(synced)} slash commands globally!")

        # --- Core Slash Commands ---

        @self.tree.command(name="login", description="Connect your Canvas account via API token")
        async def login(interaction: discord.Interaction):
            """!
            @brief Opens the login modal to connect a Canvas account.
            """
            default_url = DEFAULT_CANVAS_URL
            if interaction.guild_id:
                cfg = self.db.get_server_config(interaction.guild_id)
                if cfg and cfg.get("canvas_url"):
                    default_url = cfg["canvas_url"]
            modal = LoginModal(self, default_url=default_url)
            await interaction.response.send_modal(modal)

        @self.tree.command(name="logout", description="Disconnect your Canvas account and delete your stored token")
        async def logout(interaction: discord.Interaction):
            """!
            @brief Disconnects student Canvas credentials.
            """
            success = self.db.delete_user(interaction.user.id)
            if success:
                await interaction.response.send_message(
                    "👋 **Logged Out**: Your Canvas credentials and enrolled course links have been removed.",
                    ephemeral=True,
                )
            else:
                await interaction.response.send_message(
                    "ℹ️ You do not have an active Canvas account linked with this bot.",
                    ephemeral=True,
                )

        @self.tree.command(name="my_courses", description="View your linked Canvas account and enrolled courses")
        async def my_courses(interaction: discord.Interaction):
            """!
            @brief Displays user Canvas profile and active sections (ephemeral).
            """
            user = self.db.get_user(interaction.user.id)
            if not user:
                await interaction.response.send_message(
                    "⚠️ You have not connected your Canvas account yet! Use `/login` to connect.",
                    ephemeral=True,
                )
                return

            enrollments = self.db.get_user_enrollments(interaction.user.id)
            embed = discord.Embed(
                title=f"📚 Canvas Profile: {user.get('canvas_name')}",
                description=(
                    f"**Canvas ID:** `{user.get('canvas_user_id')}`\n"
                    f"**LMS URL:** `{user.get('canvas_url')}`\n"
                    f"**Status:** {'🟢 Active' if user.get('is_active') else '🔴 Inactive'}"
                ),
                color=discord.Color.blue(),
            )

            courses_text = ""
            for e in enrollments[:15]:
                courses_text += f"• **{e.get('course_name')}**\n  └ Section: `{e.get('section_name')}` (ID: `{e.get('section_id')}`)\n"

            if not courses_text:
                courses_text = "No active course enrollments found."
            elif len(enrollments) > 15:
                courses_text += f"\n*...and {len(enrollments) - 15} more sections.*"

            embed.add_field(name="Enrolled Courses & Sections", value=courses_text, inline=False)
            await interaction.response.send_message(embed=embed, ephemeral=True)

        async def course_autocomplete(
            interaction: discord.Interaction, current: str
        ) -> List[app_commands.Choice[str]]:
            enrollments = self.db.get_user_enrollments(interaction.user.id)
            seen = {}
            for e in enrollments:
                cid = str(e["course_id"])
                cname = e.get("course_name", f"Course {cid}")
                if cid not in seen:
                    seen[cid] = cname
            choices = [
                app_commands.Choice(name=cname[:100], value=cid)
                for cid, cname in seen.items()
                if current.lower() in cname.lower() or current in cid
            ]
            return choices[:25]

        @self.tree.command(name="my_assignments", description="View Canvas assignments with customizable filters")
        @app_commands.describe(
            course="Filter assignments for a specific course",
            include_undated="Include assignments without a due date (default: False)",
            past_due="Show assignments past their deadline (default: False)",
            completed_only="Show only completed assignments (default: False)",
        )
        @app_commands.autocomplete(course=course_autocomplete)
        async def my_assignments(
            interaction: discord.Interaction,
            course: Optional[str] = None,
            include_undated: bool = False,
            past_due: bool = False,
            completed_only: bool = False,
        ):
            """!
            @brief Displays user assignments filtered by status or course (ephemeral).
            """
            user = self.db.get_user(interaction.user.id)
            if not user:
                await interaction.response.send_message(
                    "⚠️ You have not connected your Canvas account yet! Use `/login` to connect.",
                    ephemeral=True,
                )
                return

            await interaction.response.defer(ephemeral=True, thinking=True)
            enrollments = self.db.get_user_enrollments(interaction.user.id)
            course_map = {e["course_id"]: e["course_name"] for e in enrollments}

            if course and course.strip().isdigit():
                target_course_ids = [int(course.strip())]
            elif course:
                target_course_ids = [
                    e["course_id"] for e in enrollments if course.lower() in e.get("course_name", "").lower()
                ]
            else:
                target_course_ids = list({e["course_id"] for e in enrollments})

            all_assignments = []

            async def _fetch_course(cid):
                try:
                    return await async_fetch_assignments(
                        user["canvas_url"], user["canvas_token"], cid, uncompleted_only=False
                    )
                except Exception:
                    return []

            results = await asyncio.gather(*[_fetch_course(cid) for cid in target_course_ids[:10]])
            for assignments in results:
                all_assignments.extend(assignments)

            now = datetime.now(timezone.utc)
            filtered = []
            for a in all_assignments:
                if course and course.strip().isdigit() and a["course_id"] != int(course.strip()):
                    continue

                if completed_only:
                    if not a.get("is_completed"):
                        continue
                else:
                    if a.get("is_completed"):
                        continue
                    if a.get("is_locked"):
                        continue

                due_at_str = a.get("due_at")
                if not due_at_str:
                    if not include_undated or past_due:
                        continue
                else:
                    due_dt = parse_canvas_date(due_at_str)
                    is_overdue = now > due_dt if due_dt else False

                    if past_due and not is_overdue:
                        continue
                    elif not past_due and is_overdue:
                        continue

                filtered.append(a)

            def sort_key(a):
                due = a.get("due_at")
                if not due:
                    return "9999-99-99" if not past_due else "0000-00-00"
                return due

            filtered.sort(key=sort_key, reverse=past_due)

            tags = []
            tags.append("Completed" if completed_only else "Pending")
            tags.append("Past Due" if past_due else "Upcoming")
            if include_undated:
                tags.append("Undated Included")
            if course:
                c_label = course_map.get(int(course)) if course.isdigit() else course
                if c_label:
                    tags.append(c_label[:30])

            tag_str = " • ".join(tags)
            embed_color = (
                discord.Color.green() if completed_only else (discord.Color.red() if past_due else discord.Color.purple())
            )

            if not filtered:
                await interaction.followup.send(
                    f"🎉 **No assignments found** matching filter: *{tag_str}*.",
                    ephemeral=True,
                )
                return

            embed = discord.Embed(
                title=f"📝 Canvas Assignments ({tag_str})",
                description=f"Showing **{min(len(filtered), 15)}** of **{len(filtered)}** task(s) for **{user.get('canvas_name')}**:",
                color=embed_color,
            )

            for a in filtered[:15]:
                due_str = "No due date"
                if a.get("due_at"):
                    due_dt = parse_canvas_date(a["due_at"])
                    if due_dt:
                        ts = int(due_dt.timestamp())
                        due_str = f"<t:{ts}:F> (<t:{ts}:R>)"
                    else:
                        due_str = a["due_at"]

                pts = f" | {a['points_possible']} pts" if a.get("points_possible") is not None else ""
                c_name = course_map.get(a.get("course_id"))
                course_line = f"📚 **Course:** {c_name}\n" if c_name else ""
                url = a.get("url")
                name_link = f"[{a.get('name')}]({url})" if url else a.get("name")
                status_icon = "✅ Completed" if a.get("is_completed") else ("⚠️ Past Due" if past_due else "⏳ Pending")

                embed.add_field(
                    name=a.get("name", "Assignment")[:100],
                    value=f"🔗 {name_link}\n{course_line}⏰ **Due:** {due_str}{pts} ({status_icon})",
                    inline=False,
                )

            await interaction.followup.send(embed=embed, ephemeral=True)

        async def _todo_handler(interaction: discord.Interaction):
            user = self.db.get_user(interaction.user.id)
            if not user:
                await interaction.response.send_message(
                    "⚠️ You have not connected your Canvas account yet! Use `/login` to connect.",
                    ephemeral=True,
                )
                return

            await interaction.response.defer(ephemeral=True, thinking=True)

            try:
                todos = await async_fetch_todo_items(user["canvas_url"], user["canvas_token"])
            except Exception as e:
                await interaction.followup.send(
                    f"❌ Failed to fetch To-Do items from Canvas ({e}).",
                    ephemeral=True,
                )
                return

            if not todos:
                await interaction.followup.send(
                    "🎉 **All caught up!** You have no pending items on your Canvas To-Do list.",
                    ephemeral=True,
                )
                return

            def sort_key(t):
                return t.get("due_at") or "9999-99-99"

            todos.sort(key=sort_key)

            embed = discord.Embed(
                title="📋 Canvas To-Do List",
                description=f"Showing **{len(todos)}** active To-Do item(s) for **{user.get('canvas_name')}**:",
                color=discord.Color.teal(),
            )

            enrollments = self.db.get_user_enrollments(interaction.user.id)
            course_map = {e["course_id"]: e["course_name"] for e in enrollments}

            for item in todos[:15]:
                due_str = "No due date"
                if item.get("due_at"):
                    due_dt = parse_canvas_date(item["due_at"])
                    if due_dt:
                        ts = int(due_dt.timestamp())
                        due_str = f"<t:{ts}:F> (<t:{ts}:R>)"
                    else:
                        due_str = item["due_at"]

                pts = f" | {item['points_possible']} pts" if item.get("points_possible") is not None else ""
                c_name = course_map.get(item.get("course_id"))
                course_line = f"📚 **Course:** {c_name}\n" if c_name else ""
                url = item.get("url")
                name_link = f"[{item.get('name')}]({url})" if url else item.get("name")

                embed.add_field(
                    name=item.get("name", "Task")[:100],
                    value=f"🔗 {name_link}\n{course_line}⏰ **Due:** {due_str}{pts}",
                    inline=False,
                )

            await interaction.followup.send(embed=embed, ephemeral=True)

        @self.tree.command(name="todo", description="View your current Canvas To-Do list items")
        async def todo(interaction: discord.Interaction):
            """!
            @brief Displays user To-Do list from Canvas API (ephemeral).
            """
            await _todo_handler(interaction)

        @self.tree.command(name="deadlines", description="View upcoming assignment deadlines sorted chronologically")
        @app_commands.describe(
            days="Display deadlines for the next N days (default: 14)",
            course="Optional course filter",
        )
        @app_commands.autocomplete(course=course_autocomplete)
        async def deadlines(
            interaction: discord.Interaction,
            days: int = 14,
            course: Optional[str] = None,
        ):
            """!
            @brief Lists upcoming deadlines across enrolled courses in chronological order (ephemeral).
            """
            user = self.db.get_user(interaction.user.id)
            if not user:
                await interaction.response.send_message(
                    "⚠️ You have not connected your Canvas account yet! Use `/login` to connect.",
                    ephemeral=True,
                )
                return

            await interaction.response.defer(ephemeral=True, thinking=True)
            enrollments = self.db.get_user_enrollments(interaction.user.id)
            course_map = {e["course_id"]: e["course_name"] for e in enrollments}

            if course and course.strip().isdigit():
                target_courses = [int(course.strip())]
            elif course:
                target_courses = [
                    e["course_id"] for e in enrollments if course.lower() in e.get("course_name", "").lower()
                ]
            else:
                target_courses = list({e["course_id"] for e in enrollments})

            all_assignments = []

            async def _fetch(cid):
                try:
                    return await async_fetch_assignments(
                        user["canvas_url"], user["canvas_token"], cid, uncompleted_only=True
                    )
                except Exception:
                    return []

            results = await asyncio.gather(*[_fetch(cid) for cid in target_courses[:10]])
            for res in results:
                all_assignments.extend(res)

            now = datetime.now(timezone.utc)
            max_dt = now + timedelta(days=max(1, days))

            upcoming = []
            for a in all_assignments:
                if a.get("is_completed") or a.get("is_locked"):
                    continue
                due_str = a.get("due_at")
                if not due_str:
                    continue
                due_dt = parse_canvas_date(due_str)
                if not due_dt:
                    continue
                if now <= due_dt <= max_dt:
                    upcoming.append((due_dt, a))

            upcoming.sort(key=lambda x: x[0])

            if not upcoming:
                await interaction.followup.send(
                    f"🎉 **No upcoming deadlines** within the next **{days} days**! You're completely up to date.",
                    ephemeral=True,
                )
                return

            embed = discord.Embed(
                title=f"⏰ Upcoming Deadlines (Next {days} Days)",
                description=f"Found **{len(upcoming)}** upcoming deadline(s) for **{user.get('canvas_name')}**:",
                color=discord.Color.orange(),
            )

            for due_dt, a in upcoming[:15]:
                ts = int(due_dt.timestamp())
                pts = f" | {a['points_possible']} pts" if a.get("points_possible") is not None else ""
                c_name = course_map.get(a.get("course_id"), f"Course {a.get('course_id')}")
                url = a.get("url")
                name_link = f"[{a.get('name')}]({url})" if url else a.get("name")

                embed.add_field(
                    name=a.get("name", "Assignment")[:100],
                    value=f"🔗 {name_link}\n📚 **Course:** {c_name}\n⏰ **Due:** <t:{ts}:F> (<t:{ts}:R>){pts}",
                    inline=False,
                )

            await interaction.followup.send(embed=embed, ephemeral=True)

        # --- Server & Section Administration ---

        @self.tree.command(
            name="server_setup",
            description="Configure server section binding and notification channels (Admin only)",
        )
        @app_commands.describe(
            section_id="The Canvas Section ID to bind to this server (e.g. 83494)",
            announcements_channel="Discord channel for course announcements",
            assignments_channel="Discord channel for assignments and deadlines",
        )
        @app_commands.default_permissions(manage_guild=True)
        async def server_setup(
            interaction: discord.Interaction,
            section_id: int,
            announcements_channel: discord.TextChannel,
            assignments_channel: discord.TextChannel,
        ):
            """!
            @brief Sets up primary server section and default announcement channels.
            """
            if not interaction.guild_id:
                await interaction.response.send_message("❌ This command must be run inside a Discord server.", ephemeral=True)
                return

            donors = self.db.get_section_donors(section_id)
            course_id = donors[0]["course_id"] if donors else None
            course_name = donors[0]["course_name"] if donors else None
            section_name = donors[0]["section_name"] if donors else f"Section {section_id}"

            self.db.save_server_config(
                guild_id=interaction.guild_id,
                section_id=section_id,
                course_id=course_id,
                course_name=course_name,
                section_name=section_name,
                announcements_channel_id=announcements_channel.id,
                assignments_channel_id=assignments_channel.id,
            )

            donor_status = (
                f"✅ **{len(donors)} eligible donor accounts** currently registered for this section."
                if donors
                else "⚠️ **No donor accounts currently registered.** Have students in this section run `/login` so the bot can fetch section updates."
            )

            embed = discord.Embed(
                title="⚙️ Server Configuration Saved",
                description=(
                    f"**Section ID:** `{section_id}` ({section_name})\n"
                    f"**Course:** {course_name or 'Auto-detected upon donor login'}\n"
                    f"**Announcements Channel:** {announcements_channel.mention}\n"
                    f"**Assignments Channel:** {assignments_channel.mention}\n\n"
                    f"{donor_status}"
                ),
                color=discord.Color.green(),
            )
            await interaction.response.send_message(embed=embed)

        @self.tree.command(
            name="section_setup",
            description="Bind a dedicated section to its channel with optional role ping (Admin only)",
        )
        @app_commands.describe(
            section_id="The Canvas Section ID (e.g. 83494)",
            channel="Dedicated channel for this section's announcements & updates",
            role="Optional role to mention/restrict visibility for this section",
        )
        @app_commands.default_permissions(manage_guild=True)
        async def section_setup(
            interaction: discord.Interaction,
            section_id: int,
            channel: discord.TextChannel,
            role: Optional[discord.Role] = None,
        ):
            """!
            @brief Configures multi-section routing with dedicated section channel and role pings.
            """
            if not interaction.guild_id:
                await interaction.response.send_message("❌ This command must be run inside a Discord server.", ephemeral=True)
                return

            donors = self.db.get_section_donors(section_id)
            course_id = donors[0]["course_id"] if donors else None
            course_name = donors[0]["course_name"] if donors else None
            section_name = donors[0]["section_name"] if donors else f"Section {section_id}"

            self.db.save_server_section(
                guild_id=interaction.guild_id,
                section_id=section_id,
                course_id=course_id,
                course_name=course_name,
                section_name=section_name,
                channel_id=channel.id,
                role_id=role.id if role else None,
            )

            role_str = role.mention if role else "None (No role ping)"
            embed = discord.Embed(
                title="📌 Section Channel Bound",
                description=(
                    f"**Section:** `{section_id}` ({section_name})\n"
                    f"**Course:** {course_name or 'Auto-detected'}\n"
                    f"**Channel:** {channel.mention}\n"
                    f"**Role Ping:** {role_str}\n"
                    f"**Donors:** {len(donors)} active donor token(s)"
                ),
                color=discord.Color.blue(),
            )
            await interaction.response.send_message(embed=embed)

        @self.tree.command(name="section_list", description="List all configured section channels in this server")
        async def section_list(interaction: discord.Interaction):
            """!
            @brief Lists all section-to-channel bindings for this server.
            """
            if not interaction.guild_id:
                await interaction.response.send_message("❌ This command must be run inside a Discord server.", ephemeral=True)
                return

            sections = self.db.get_server_sections(interaction.guild_id)
            if not sections:
                await interaction.response.send_message(
                    "ℹ️ No dedicated section channels configured yet. Use `/section_setup`.",
                    ephemeral=True,
                )
                return

            embed = discord.Embed(title="📚 Configured Course Sections", color=discord.Color.blue())
            for s in sections:
                role_txt = f"<@&{s['role_id']}>" if s.get("role_id") else "No role"
                donors = self.db.get_section_donors(s["section_id"])
                embed.add_field(
                    name=f"Section {s['section_id']}: {s.get('section_name') or 'Section'}",
                    value=(
                        f"Channel: <#{s['channel_id']}>\n"
                        f"Role: {role_txt}\n"
                        f"Course: {s.get('course_name') or 'N/A'}\n"
                        f"Active Donors: {len(donors)}"
                    ),
                    inline=False,
                )
            await interaction.response.send_message(embed=embed)

        @self.tree.command(name="section_unlink", description="Remove a section channel binding (Admin only)")
        @app_commands.describe(section_id="The Canvas Section ID to unlink")
        @app_commands.default_permissions(manage_guild=True)
        async def section_unlink(interaction: discord.Interaction, section_id: int):
            """!
            @brief Removes a section-to-channel mapping.
            """
            if not interaction.guild_id:
                await interaction.response.send_message("❌ This command must be run inside a Discord server.", ephemeral=True)
                return

            removed = self.db.delete_server_section(interaction.guild_id, section_id)
            if removed:
                await interaction.response.send_message(f"🗑️ Section `{section_id}` unlinked successfully.", ephemeral=True)
            else:
                await interaction.response.send_message(f"ℹ️ Section `{section_id}` was not configured.", ephemeral=True)

        @self.tree.command(name="set_command_channel", description="Set dedicated channel for Canvas bot commands (Admin only)")
        @app_commands.describe(channel="The channel for slash commands (e.g. #canvas-bot)")
        @app_commands.default_permissions(manage_guild=True)
        async def set_command_channel(interaction: discord.Interaction, channel: discord.TextChannel):
            """!
            @brief Configures the dedicated command channel restriction (#canvas-bot).
            """
            if not interaction.guild_id:
                await interaction.response.send_message("❌ This command must be run inside a Discord server.", ephemeral=True)
                return

            self.db.update_server_settings(guild_id=interaction.guild_id, command_channel_id=channel.id)
            await interaction.response.send_message(
                f"✅ **Command Channel Set**: General bot commands are now restricted to {channel.mention}.",
                ephemeral=True,
            )

        @self.tree.command(
            name="set_school_announcements",
            description="Configure school-wide announcements channel and role ping (Admin only)",
        )
        @app_commands.describe(
            channel="Discord channel for institution-wide announcements",
            role="Optional role to mention (e.g. @school announcement)",
        )
        @app_commands.default_permissions(manage_guild=True)
        async def set_school_announcements(
            interaction: discord.Interaction,
            channel: discord.TextChannel,
            role: Optional[discord.Role] = None,
        ):
            """!
            @brief Configures global / school-wide announcements channel with role ping.
            """
            if not interaction.guild_id:
                await interaction.response.send_message("❌ This command must be run inside a Discord server.", ephemeral=True)
                return

            role_id = role.id if role else None
            self.db.update_server_settings(
                guild_id=interaction.guild_id,
                school_announcements_channel_id=channel.id,
                school_announcements_role_id=role_id,
            )
            role_str = role.mention if role else "No role ping"
            await interaction.response.send_message(
                f"✅ **School Announcements Configured**:\n"
                f"• Channel: {channel.mention}\n"
                f"• Role Ping: {role_str}",
                ephemeral=True,
            )

        @self.tree.command(name="set_canvas_url", description="Set default Canvas LMS instance URL for this server (Admin only)")
        @app_commands.describe(url="Base Canvas URL (e.g. https://canvas.instructure.com)")
        @app_commands.default_permissions(manage_guild=True)
        async def set_canvas_url(interaction: discord.Interaction, url: str):
            """!
            @brief Configures institution-specific Canvas LMS URL for multi-school compatibility.
            """
            if not interaction.guild_id:
                await interaction.response.send_message("❌ This command must be run inside a Discord server.", ephemeral=True)
                return

            clean_url = url.strip().rstrip("/")
            self.db.update_server_settings(guild_id=interaction.guild_id, canvas_url=clean_url)
            await interaction.response.send_message(
                f"✅ **Canvas LMS URL Updated**: Default institution set to `{clean_url}` for this server.",
                ephemeral=True,
            )

        @self.tree.command(name="server_status", description="View the current Canvas configuration for this server")
        async def server_status(interaction: discord.Interaction):
            """!
            @brief Displays detailed server settings, section channels, and donor statistics.
            """
            if not interaction.guild_id:
                await interaction.response.send_message("❌ This command must be run inside a Discord server.", ephemeral=True)
                return

            cfg = self.db.get_server_config(interaction.guild_id)
            sections = self.db.get_server_sections(interaction.guild_id)

            if not cfg and not sections:
                await interaction.response.send_message(
                    "ℹ️ This server has not been configured yet. An admin can run `/server_setup` or `/section_setup`.",
                    ephemeral=True,
                )
                return

            embed = discord.Embed(title="⚙️ Server Canvas Status", color=discord.Color.blue())
            if cfg:
                embed.add_field(
                    name="Canvas LMS Endpoint",
                    value=f"`{cfg.get('canvas_url') or DEFAULT_CANVAS_URL}`",
                    inline=False,
                )
                if cfg.get("command_channel_id"):
                    embed.add_field(name="Command Channel", value=f"<#{cfg['command_channel_id']}>", inline=True)
                if cfg.get("school_announcements_channel_id"):
                    role_str = (
                        f" (<@&{cfg['school_announcements_role_id']}>)" if cfg.get("school_announcements_role_id") else ""
                    )
                    embed.add_field(
                        name="School Announcements",
                        value=f"<#{cfg['school_announcements_channel_id']}>{role_str}",
                        inline=True,
                    )
                if cfg.get("section_id"):
                    donors = self.db.get_section_donors(cfg["section_id"])
                    embed.add_field(
                        name="Primary Section",
                        value=f"`{cfg['section_id']}` ({len(donors)} donors)",
                        inline=True,
                    )

            if sections:
                lines = []
                for s in sections:
                    role_suffix = f" (Role: <@&{s['role_id']}>)" if s.get("role_id") else ""
                    lines.append(f"• Section `{s['section_id']}` -> <#{s['channel_id']}>{role_suffix}")
                sec_desc = "\n".join(lines)
                embed.add_field(name=f"Configured Sections ({len(sections)})", value=sec_desc, inline=False)

            await interaction.response.send_message(embed=embed)

        @self.tree.command(name="server_unlink", description="Reset Canvas settings for this server (Admin only)")
        @app_commands.default_permissions(manage_guild=True)
        async def server_unlink(interaction: discord.Interaction):
            """!
            @brief Resets all server configs and section bindings.
            """
            if not interaction.guild_id:
                await interaction.response.send_message("❌ This command must be run inside a Discord server.", ephemeral=True)
                return

            removed = self.db.delete_server_config(interaction.guild_id)
            if removed:
                await interaction.response.send_message("🗑️ Server Canvas configuration removed successfully.", ephemeral=True)
            else:
                await interaction.response.send_message("ℹ️ No configuration found for this server.", ephemeral=True)

        # --- Gamification & Leaderboard System ---

        @self.tree.command(name="leaderboard", description="Display top students ranked by earned Canvas activity coins")
        async def leaderboard(interaction: discord.Interaction):
            """!
            @brief Displays the public leaderboard of top students by earned coins.
            """
            leaders = self.db.get_top_leaderboard(limit=10)
            if not leaders:
                await interaction.response.send_message(
                    "🪙 **Canvas Leaderboard**\nNo coins earned yet. "
                    "Submit assignments on Canvas and earn coins based on your submission order rank!",
                    ephemeral=False,
                )
                return

            embed = discord.Embed(
                title="🏆 Canvas Activity Leaderboard",
                description="Top students rewarded for early, verified assignment submissions:",
                color=discord.Color.gold(),
            )

            medals = ["🥇", "🥈", "🥉"]
            for idx, entry in enumerate(leaders):
                rank_str = medals[idx] if idx < 3 else f"`#{idx + 1}`"
                canvas_name = entry.get("canvas_name") or f"Student <@{entry['discord_id']}>"
                coins = entry["balance"]
                total = entry["total_earned"]
                embed.add_field(
                    name=f"{rank_str} {canvas_name}",
                    value=f"🪙 **{coins} coins** (Total Earned: `{total}`)",
                    inline=False,
                )

            embed.set_footer(text="Points awarded via verified Canvas API submissions (1st: 100, 2nd: 75, 3rd: 50, 4th+: 25).")
            await interaction.response.send_message(embed=embed, ephemeral=False)

        @self.tree.command(name="coins", description="Check your current coin balance and submission stats")
        async def coins(interaction: discord.Interaction):
            """!
            @brief Displays user's coin balance and lifetime earned total (ephemeral).
            """
            econ = self.db.get_user_economy(interaction.user.id)
            embed = discord.Embed(
                title="🪙 Your Activity Wallet",
                description=(
                    f"**Current Balance:** `{econ['balance']} coins`\n"
                    f"**Lifetime Earned:** `{econ['total_earned']} coins`"
                ),
                color=discord.Color.teal(),
            )
            embed.set_footer(text="Use your coins in casual minigames like /flipcoin!")
            await interaction.response.send_message(embed=embed, ephemeral=True)

        @self.tree.command(name="claim_coins", description="Verify recent Canvas submissions and claim submission rank coins")
        async def claim_coins(interaction: discord.Interaction):
            """!
            @brief On-demand check to verify student assignment submissions and award order-based points.
            """
            user = self.db.get_user(interaction.user.id)
            if not user:
                await interaction.response.send_message(
                    "⚠️ You have not connected your Canvas account yet! Use `/login` to connect.",
                    ephemeral=True,
                )
                return

            await interaction.response.defer(ephemeral=True, thinking=True)
            enrollments = self.db.get_user_enrollments(interaction.user.id)
            distinct_courses = list({e["course_id"] for e in enrollments})

            total_awarded_coins = 0
            verified_count = 0

            for cid in distinct_courses:
                assignments = self.cached_assignments_pool.get(cid)
                if not assignments:
                    try:
                        assignments = await async_fetch_assignments(user["canvas_url"], user["canvas_token"], cid)
                    except Exception:
                        continue

                for a in assignments:
                    ass_id = str(a["id"])
                    if self.db.is_submission_recorded(ass_id, interaction.user.id):
                        continue

                    try:
                        sub = await async_get_user_submission(
                            user["canvas_url"], user["canvas_token"], cid, int(ass_id)
                        )
                    except Exception:
                        continue

                    if sub and sub.get("is_completed") and sub.get("submitted_at"):
                        awarded, rank, pts = self.db.record_submission_and_award_points(
                            ass_id, interaction.user.id, cid, sub["submitted_at"]
                        )
                        if awarded:
                            verified_count += 1
                            total_awarded_coins += pts

            if verified_count > 0:
                econ = self.db.get_user_economy(interaction.user.id)
                await interaction.followup.send(
                    f"🎉 **Verified {verified_count} new submission(s)**!\n"
                    f"Awarded **{total_awarded_coins} coins** based on submission ranking.\n"
                    f"New Wallet Balance: **{econ['balance']} coins** 🪙",
                    ephemeral=True,
                )
            else:
                await interaction.followup.send(
                    "ℹ️ No new unverified submissions found on Canvas. Turn in assignments on Canvas to earn rank coins!",
                    ephemeral=True,
                )

        @self.tree.command(name="flipcoin", description="Bet your earned Canvas coins on a coin flip")
        @app_commands.describe(
            choice="Choose Heads or Tails",
            bet="Number of coins to bet (minimum 1)",
        )
        @app_commands.choices(
            choice=[
                app_commands.Choice(name="Heads", value="heads"),
                app_commands.Choice(name="Tails", value="tails"),
            ]
        )
        async def flipcoin(interaction: discord.Interaction, choice: app_commands.Choice[str], bet: int):
            """!
            @brief Minigame: flip a coin and double or lose activity coins.
            """
            if bet <= 0:
                await interaction.response.send_message("❌ Bet must be at least 1 coin.", ephemeral=True)
                return

            econ = self.db.get_user_economy(interaction.user.id)
            if econ["balance"] < bet:
                await interaction.response.send_message(
                    f"❌ **Insufficient Coins**: You have **{econ['balance']} coins**, but tried to bet **{bet}**.",
                    ephemeral=True,
                )
                return

            outcome = random.choice(["heads", "tails"])
            won = choice.value.lower() == outcome

            if won:
                new_bal = self.db.update_user_coins(interaction.user.id, bet)
                embed = discord.Embed(
                    title="🪙 Coin Flip: You Won!",
                    description=(
                        f"The coin landed on **{outcome.capitalize()}**!\n"
                        f"🎉 You won **+{bet} coins**!\n\n"
                        f"**New Balance:** `{new_bal} coins`"
                    ),
                    color=discord.Color.green(),
                )
            else:
                new_bal = self.db.update_user_coins(interaction.user.id, -bet)
                embed = discord.Embed(
                    title="🪙 Coin Flip: You Lost!",
                    description=(
                        f"The coin landed on **{outcome.capitalize()}**!\n"
                        f"💥 You lost **-{bet} coins**.\n\n"
                        f"**New Balance:** `{new_bal} coins`"
                    ),
                    color=discord.Color.red(),
                )

            await interaction.response.send_message(embed=embed)

        @self.tree.command(name="sync_now", description="Manually trigger an immediate sync of announcements & assignments")
        async def sync_now(interaction: discord.Interaction):
            """!
            @brief Manually triggers an immediate synchronization pass.
            """
            await interaction.response.defer(ephemeral=True, thinking=True)
            counts = await self.sync_canvas_data()
            await interaction.followup.send(
                f"✅ **Sync Complete**:\n"
                f"• Server Announcements: **{counts['server_announcements']}**\n"
                f"• Server Assignments: **{counts['server_assignments']}**\n"
                f"• Section Announcements: **{counts['section_announcements']}**\n"
                f"• Section Assignments: **{counts['section_assignments']}**\n"
                f"• Submissions Verified: **{counts['submissions_verified']}**\n"
                f"• User DMs (Legacy): **{counts['user_dms']}**",
                ephemeral=True,
            )

    # --- Canvas Sync Engine ---

    async def sync_canvas_data(self) -> Dict[str, int]:
        """!
        @brief Executes one full sync pass across all authorized servers and sections.
        @details Routes announcements and assignments to server and section channels,
                 verifies submissions for gamification, and deprecates unsolicited direct DMs.
        @return Dictionary containing counts of delivered items.
        """
        counts = {
            "user_dms": 0,
            "server_announcements": 0,
            "server_assignments": 0,
            "section_announcements": 0,
            "section_assignments": 0,
            "submissions_verified": 0,
            "reminders": 0,
        }

        pending_user_ann: List[Tuple[int, str]] = []
        pending_server_ann: List[Tuple[int, str]] = []
        pending_server_ass: List[Tuple[int, str]] = []
        pending_reminders: List[Tuple[int, str]] = []

        enrollment_cache: Dict[int, List[int]] = {}
        course_announcements_pool: Dict[int, List[Dict[str, Any]]] = {}

        # 1. Sync Server Configs (Legacy / Primary section)
        server_configs = self.db.get_all_server_configs()
        for cfg in server_configs:
            guild_id = cfg["guild_id"]
            if not self._is_guild_authorized(guild_id):
                continue

            section_id = cfg.get("section_id")
            if not section_id:
                continue

            donor = self.db.get_next_donor_for_server(guild_id, section_id)
            if not donor:
                continue

            course_id = donor.get("course_id") or cfg.get("course_id")
            if not course_id:
                continue

            token = donor["canvas_token"]
            url = donor["canvas_url"]

            # Announcements
            try:
                announcements = await async_fetch_announcements(url, token, [course_id])
                course_announcements_pool[course_id] = announcements
                ann_channel = self.get_channel(cfg.get("announcements_channel_id", 0))
                school_ann_channel = (
                    self.get_channel(cfg["school_announcements_channel_id"])
                    if cfg.get("school_announcements_channel_id")
                    else None
                )

                for ann in announcements:
                    ann_id = ann["id"]
                    is_global = not ann.get("is_section_specific", False)

                    # Post to School Announcements if global and configured
                    if is_global and school_ann_channel:
                        if (guild_id, f"school_{ann_id}") not in self.server_announcements_cache:
                            role_mention = (
                                f"<@&{cfg['school_announcements_role_id']}>\n"
                                if cfg.get("school_announcements_role_id")
                                else ""
                            )
                            embed = discord.Embed(
                                title=f"📢 Global School Announcement: {ann['title']}",
                                url=ann.get("url") or None,
                                description=ann.get("message_clean") or "No content preview available.",
                                color=discord.Color.gold(),
                            )
                            embed.add_field(name="Author", value=ann.get("author", "Administration"), inline=True)
                            if ann.get("posted_at"):
                                embed.set_footer(text=f"Posted: {ann['posted_at']}")
                            try:
                                await school_ann_channel.send(content=role_mention or None, embed=embed)
                                self.server_announcements_cache.add((guild_id, f"school_{ann_id}"))
                                pending_server_ann.append((guild_id, f"school_{ann_id}"))
                            except discord.HTTPException:
                                pass

                    # Post to regular server announcements channel
                    if ann_channel and (guild_id, ann_id) not in self.server_announcements_cache:
                        embed = discord.Embed(
                            title=f"📢 {ann['title']}",
                            url=ann.get("url") or None,
                            description=ann.get("message_clean") or "No content preview available.",
                            color=discord.Color.gold(),
                        )
                        embed.add_field(
                            name="Course", value=cfg.get("course_name") or f"Course {course_id}", inline=True
                        )
                        embed.add_field(name="Author", value=ann.get("author", "Instructor"), inline=True)
                        if ann.get("posted_at"):
                            embed.set_footer(text=f"Posted: {ann['posted_at']}")

                        try:
                            await ann_channel.send(embed=embed)
                            self.server_announcements_cache.add((guild_id, ann_id))
                            pending_server_ann.append((guild_id, ann_id))
                            counts["server_announcements"] += 1
                        except discord.HTTPException as e:
                            print(f"[Bot] Failed to send announcement to server {guild_id}: {e}")
            except Exception as e:
                print(f"[Bot] Error syncing announcements for server {guild_id}: {e}")

            # Assignments
            try:
                assignments = await async_fetch_assignments(url, token, course_id)
                self.cached_assignments_pool[course_id] = assignments
                ass_channel = self.get_channel(cfg.get("assignments_channel_id", 0))
                if ass_channel:
                    for a in assignments:
                        if a.get("is_locked"):
                            continue
                        ass_id = a["id"]
                        if (guild_id, ass_id) not in self.server_assignments_cache:
                            due_str = "No due date"
                            if a.get("due_at"):
                                due_dt = parse_canvas_date(a["due_at"])
                                if due_dt:
                                    ts = int(due_dt.timestamp())
                                    due_str = f"<t:{ts}:F> (<t:{ts}:R>)"
                                else:
                                    due_str = a["due_at"]

                            embed = discord.Embed(
                                title=f"📝 New Assignment: {a['name']}",
                                url=a.get("url") or None,
                                description=a.get("description_clean") or "No description provided.",
                                color=discord.Color.blue(),
                            )
                            embed.add_field(
                                name="Course", value=cfg.get("course_name") or f"Course {course_id}", inline=True
                            )
                            embed.add_field(name="Due Date", value=due_str, inline=True)
                            if a.get("points_possible") is not None:
                                embed.add_field(name="Points", value=f"{a['points_possible']} pts", inline=True)

                            try:
                                await ass_channel.send(embed=embed)
                                self.server_assignments_cache.add((guild_id, ass_id))
                                pending_server_ass.append((guild_id, ass_id))
                                counts["server_assignments"] += 1
                            except discord.HTTPException as e:
                                print(f"[Bot] Failed to send assignment to server {guild_id}: {e}")
            except Exception as e:
                print(f"[Bot] Error syncing assignments for server {guild_id}: {e}")

        # 2. Sync Dedicated Multiple Server Sections
        server_sections = self.db.get_all_server_sections()
        for sec in server_sections:
            guild_id = sec["guild_id"]
            if not self._is_guild_authorized(guild_id):
                continue

            sec_id = sec["section_id"]
            channel_id = sec["channel_id"]
            role_id = sec.get("role_id")
            channel = self.get_channel(channel_id)
            if not channel:
                continue

            donor = self.db.get_next_donor_for_server_section(guild_id, sec_id)
            if not donor:
                continue

            course_id = donor.get("course_id") or sec.get("course_id")
            if not course_id:
                continue

            token = donor["canvas_token"]
            url = donor["canvas_url"]
            role_mention = f"<@&{role_id}>\n" if role_id else ""

            # Section Announcements
            try:
                announcements = await async_fetch_announcements(url, token, [course_id])
                for ann in announcements:
                    ann_key = f"sec_{sec_id}_{ann['id']}"
                    if (guild_id, ann_key) not in self.server_announcements_cache:
                        embed = discord.Embed(
                            title=f"📢 {sec.get('section_name') or f'Section {sec_id}'}: {ann['title']}",
                            url=ann.get("url") or None,
                            description=ann.get("message_clean") or "No preview available.",
                            color=discord.Color.purple(),
                        )
                        embed.add_field(name="Course", value=sec.get("course_name") or f"Course {course_id}", inline=True)
                        embed.add_field(name="Author", value=ann.get("author", "Instructor"), inline=True)
                        if ann.get("posted_at"):
                            embed.set_footer(text=f"Posted: {ann['posted_at']}")

                        try:
                            await channel.send(content=role_mention or None, embed=embed)
                            self.server_announcements_cache.add((guild_id, ann_key))
                            pending_server_ann.append((guild_id, ann_key))
                            counts["section_announcements"] += 1
                        except discord.HTTPException:
                            pass
            except Exception as e:
                print(f"[Bot] Error syncing section {sec_id} announcements: {e}")

            # Section Assignments
            try:
                assignments = await async_fetch_assignments(url, token, course_id)
                self.cached_assignments_pool[course_id] = assignments
                for a in assignments:
                    if a.get("is_locked"):
                        continue
                    ass_key = f"sec_{sec_id}_{a['id']}"
                    if (guild_id, ass_key) not in self.server_assignments_cache:
                        due_str = "No due date"
                        if a.get("due_at"):
                            due_dt = parse_canvas_date(a["due_at"])
                            if due_dt:
                                ts = int(due_dt.timestamp())
                                due_str = f"<t:{ts}:F> (<t:{ts}:R>)"
                            else:
                                due_str = a["due_at"]

                        embed = discord.Embed(
                            title=f"📝 [{sec.get('section_name') or f'Section {sec_id}'}] New Assignment: {a['name']}",
                            url=a.get("url") or None,
                            description=a.get("description_clean") or "No description provided.",
                            color=discord.Color.blue(),
                        )
                        embed.add_field(name="Course", value=sec.get("course_name") or f"Course {course_id}", inline=True)
                        embed.add_field(name="Due Date", value=due_str, inline=True)
                        if a.get("points_possible") is not None:
                            embed.add_field(name="Points", value=f"{a['points_possible']} pts", inline=True)

                        try:
                            await channel.send(content=role_mention or None, embed=embed)
                            self.server_assignments_cache.add((guild_id, ass_key))
                            pending_server_ass.append((guild_id, ass_key))
                            counts["section_assignments"] += 1
                        except discord.HTTPException:
                            pass
            except Exception as e:
                print(f"[Bot] Error syncing section {sec_id} assignments: {e}")

        # 3. Verified Submission Gamification Engine
        distinct_course_ids = self.db.get_all_distinct_course_ids()
        for cid in distinct_course_ids:
            if cid not in self.cached_assignments_pool:
                donor = self.db.get_any_active_token_for_course(cid)
                if donor:
                    try:
                        self.cached_assignments_pool[cid] = await async_fetch_assignments(
                            donor["canvas_url"], donor["canvas_token"], cid
                        )
                    except Exception:
                        self.cached_assignments_pool[cid] = []

            assignments = self.cached_assignments_pool.get(cid, [])
            enrolled_student_ids = enrollment_cache.get(cid)
            if enrolled_student_ids is None:
                enrolled_student_ids = self.db.get_users_enrolled_in_course(cid)
                enrollment_cache[cid] = enrolled_student_ids

            if not enrolled_student_ids or not assignments:
                continue

            for a in assignments[:10]:
                ass_id = str(a["id"])
                for student_id in enrolled_student_ids:
                    if self.db.is_submission_recorded(ass_id, student_id):
                        continue

                    student = self.db.get_user(student_id)
                    if not student or not student.get("is_active"):
                        continue

                    try:
                        sub = await async_get_user_submission(
                            student["canvas_url"], student["canvas_token"], cid, int(ass_id)
                        )
                        if sub and sub.get("is_completed") and sub.get("submitted_at"):
                            awarded, rank, pts = self.db.record_submission_and_award_points(
                                ass_id, student_id, cid, sub["submitted_at"]
                            )
                            if awarded:
                                counts["submissions_verified"] += 1
                    except Exception:
                        pass

        # 4. Direct DM Broadcasts (Deprecated)
        if not self.enable_dm_broadcasts:
            # Unsolicited DMs deprecated: students check /deadlines, /my_assignments, and /todo on-demand.
            pass
        else:
            warnings.warn(
                "Direct DM broadcasts are deprecated. Use channel pings or /deadlines instead.",
                DeprecationWarning,
                stacklevel=2,
            )
            # Legacy DM path
            for course_id in distinct_course_ids:
                announcements = course_announcements_pool.get(course_id)
                if not announcements:
                    donor = self.db.get_any_active_token_for_course(course_id)
                    if donor:
                        try:
                            announcements = await async_fetch_announcements(
                                donor["canvas_url"], donor["canvas_token"], [course_id]
                            )
                        except Exception:
                            announcements = []

                if announcements:
                    enrolled_students = enrollment_cache.get(course_id)
                    if enrolled_students is None:
                        enrolled_students = self.db.get_users_enrolled_in_course(course_id)
                        enrollment_cache[course_id] = enrolled_students

                    for ann in announcements:
                        ann_id = ann["id"]
                        for student_id in enrolled_students:
                            if (student_id, ann_id) in self.user_announcements_cache:
                                continue
                            try:
                                user = self.get_user(student_id) or await self.fetch_user(student_id)
                                if user:
                                    embed = discord.Embed(
                                        title=f"📢 New Course Announcement: {ann['title']}",
                                        url=ann.get("url") or None,
                                        description=ann.get("message_clean") or "No preview.",
                                        color=discord.Color.gold(),
                                    )
                                    await user.send(embed=embed)
                                    self.user_announcements_cache.add((student_id, ann_id))
                                    pending_user_ann.append((student_id, ann_id))
                                    counts["user_dms"] += 1
                            except discord.Forbidden:
                                self.user_announcements_cache.add((student_id, ann_id))
                                pending_user_ann.append((student_id, ann_id))
                            except Exception:
                                pass

            # Legacy 12h deadline reminder DMs
            now = datetime.now(timezone.utc)
            for course_id in distinct_course_ids:
                assignments = self.cached_assignments_pool.get(course_id, [])
                enrolled_students = enrollment_cache.get(course_id) or self.db.get_users_enrolled_in_course(course_id)
                for a in assignments:
                    if a.get("is_locked"):
                        continue
                    due_at_str = a.get("due_at")
                    if not due_at_str:
                        continue
                    due_dt = parse_canvas_date(due_at_str)
                    if not due_dt:
                        continue
                    time_left = (due_dt - now).total_seconds()
                    if not (0 < time_left <= 12 * 3600):
                        continue

                    ass_id = str(a["id"])
                    for student_id in enrolled_students:
                        if (student_id, ass_id) in self.user_reminders_cache:
                            continue
                        student = self.db.get_user(student_id)
                        if not student or not student.get("is_active"):
                            continue
                        try:
                            is_done = await async_check_user_assignment_completion(
                                student["canvas_url"], student["canvas_token"], course_id, int(ass_id)
                            )
                        except Exception:
                            continue

                        if is_done:
                            self.user_reminders_cache.add((student_id, ass_id))
                            pending_reminders.append((student_id, ass_id))
                            continue

                        try:
                            user_obj = self.get_user(student_id) or await self.fetch_user(student_id)
                            if user_obj:
                                ts = int(due_dt.timestamp())
                                embed = discord.Embed(
                                    title=f"⏰ 12-Hour Deadline Reminder: {a['name']}",
                                    url=a.get("url") or None,
                                    description=f"⚠️ Due in less than 12 hours: <t:{ts}:F>",
                                    color=discord.Color.red(),
                                )
                                await user_obj.send(embed=embed)
                                counts["reminders"] += 1
                        except discord.Forbidden:
                            pass
                        except Exception:
                            pass

                        self.user_reminders_cache.add((student_id, ass_id))
                        pending_reminders.append((student_id, ass_id))

        self.db.flush_deliveries(pending_user_ann, pending_server_ann, pending_server_ass, pending_reminders)
        return counts

    @tasks.loop(minutes=5)
    async def scan_canvas_loop(self):
        """!
        @brief Recurring 5-minute task polling Canvas updates and syncing to Discord.
        """
        await self.wait_until_ready()
        try:
            print("[Scan] Starting Canvas sync pass...")
            counts = await self.sync_canvas_data()
            print(f"[Scan] Completed sync pass: {counts}")
        except Exception as e:
            print(f"[Scan] Exception during sync loop: {e}")

    @scan_canvas_loop.before_loop
    async def before_scan_loop(self):
        """!
        @brief Ensures the bot is ready before executing the polling loop.
        """
        await self.wait_until_ready()


def run():
    """!
    @brief Entrypoint to load environment credentials and start the Discord bot.
    """
    token = BOT_TOKEN
    if not token:
        raise ValueError("BOT_TOKEN is not configured. Please set BOT_TOKEN in .env.")
    bot = CanvasDiscordBot()
    bot.run(token)


if __name__ == "__main__":
    run()
