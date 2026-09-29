import os
import asyncio
from datetime import datetime, timezone
from typing import Optional, List, Dict, Any, Set, Tuple

import discord
from discord.ext import commands, tasks
from discord import app_commands
from dotenv import load_dotenv

from painterv3.db import Database, DEFAULT_DB_PATH
from painterv3.canvas_client import (
    async_validate_and_get_profile,
    async_fetch_announcements,
    async_fetch_assignments,
    async_fetch_todo_items,
    async_check_user_assignment_completion,
    parse_canvas_date,
)

# ponytail: In-memory cache sets loaded at startup for deduplication.
# Ceiling: Memory exhaustion if millions of deliveries over years.
# Upgrade path: LRU cache with Redis or TTL index in DB.

# ponytail: Polling Canvas REST API every 5 minutes.
# Ceiling: Notification delay up to polling interval.
# Upgrade path: Canvas Outgoing Webhooks or Canvas Event Streaming with an HTTP receiver.

load_dotenv()
BOT_TOKEN = os.getenv("BOT_TOKEN") or os.getenv("DISCORD_TOKEN")
DEFAULT_CANVAS_URL = os.getenv("CANVAS_API_URL", "https://tip.instructure.com")


class LoginModal(discord.ui.Modal, title="Canvas Account Login"):
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
        placeholder="e.g. https://tip.instructure.com",
        required=False,
    )

    def __init__(self, bot_instance: "CanvasDiscordBot"):
        super().__init__()
        self.bot = bot_instance

    async def on_submit(self, interaction: discord.Interaction):
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
                f"Your account is now registered with **{len(seen_courses)} active courses**.\n"
                "You will receive automatic direct messages (DMs) when announcements are posted in your courses."
            ),
            color=discord.Color.green(),
        )
        embed.add_field(name="Enrolled Courses", value=courses_summary, inline=False)
        embed.set_footer(text="Your token is securely stored and used to sync course updates.")

        await interaction.followup.send(embed=embed, ephemeral=True)


class CanvasDiscordBot(commands.Bot):
    def __init__(self, db_path: str = DEFAULT_DB_PATH):
        # Default intents are sufficient for slash commands, DMs, and channel messages
        intents = discord.Intents.default()
        super().__init__(command_prefix="!", intents=intents)

        self.db = Database(db_path=db_path)
        self.user_announcements_cache: Set[Tuple[int, str]] = set()
        self.server_announcements_cache: Set[Tuple[int, str]] = set()
        self.server_assignments_cache: Set[Tuple[int, str]] = set()
        self.user_reminders_cache: Set[Tuple[int, str]] = set()
        self.cached_assignments_pool: Dict[int, List[Dict[str, Any]]] = {}

    async def setup_hook(self):
        # Pre-load in-memory delivery caches from SQLite
        u_ann, s_ann, s_ass, u_rem = self.db.load_cache_sets()
        self.user_announcements_cache = u_ann
        self.server_announcements_cache = s_ann
        self.server_assignments_cache = s_ass
        self.user_reminders_cache = u_rem
        print(
            f"[Cache] Loaded {len(u_ann)} user deliveries, {len(s_ann)} server announcements, "
            f"{len(s_ass)} server assignments, {len(u_rem)} deadline reminders into memory."
        )

        # Register slash commands
        self._register_commands()
        try:
            print("[Bot] Syncing slash command tree...")
            synced = await asyncio.wait_for(self.tree.sync(), timeout=15.0)
            print(f"[Bot] Synced {len(synced)} slash commands: {[c.name for c in synced]}")
        except asyncio.TimeoutError:
            print("[Bot] Command sync timed out (Discord rate limit). Commands remain registered.")
        except Exception as e:
            print(f"[Bot] Failed to sync slash commands: {e}")

        # Start background polling loop
        if not self.scan_canvas_loop.is_running():
            self.scan_canvas_loop.start()

    def _register_commands(self):
        if getattr(self, "_commands_registered", False):
            return
        self._commands_registered = True

        @self.command(name="sync")
        @commands.is_owner()
        async def sync_prefix(ctx: commands.Context, guild_only: bool = True):
            """Instantly sync slash commands to current server or globally."""
            if guild_only and ctx.guild:
                self.tree.copy_global_to(guild=ctx.guild)
                synced = await self.tree.sync(guild=ctx.guild)
                await ctx.reply(f"✅ Synced {len(synced)} slash commands directly to **{ctx.guild.name}**!")
            else:
                synced = await self.tree.sync()
                await ctx.reply(f"✅ Synced {len(synced)} slash commands globally!")

        @self.tree.command(name="login", description="Connect your Canvas account via API token")
        async def login(interaction: discord.Interaction):
            modal = LoginModal(self)
            await interaction.response.send_modal(modal)

        @self.tree.command(name="logout", description="Disconnect your Canvas account and delete your stored token")
        async def logout(interaction: discord.Interaction):
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
                description=f"**Canvas ID:** `{user.get('canvas_user_id')}`\n**Status:** {'🟢 Active' if user.get('is_active') else '🔴 Inactive'}",
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
                # 1. Course filter
                if course and course.strip().isdigit() and a["course_id"] != int(course.strip()):
                    continue

                # 2. Completion filter
                if completed_only:
                    if not a.get("is_completed"):
                        continue
                else:
                    if a.get("is_completed"):
                        continue
                    if a.get("is_locked"):
                        continue

                # 3. Due date filter
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

            # Sort: nearest upcoming first, or most recently overdue first
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

            # Sort by due date
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
            await _todo_handler(interaction)

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
            if not interaction.guild_id:
                await interaction.response.send_message("❌ This command must be run inside a Discord server.", ephemeral=True)
                return

            # Look up if any user has this section to retrieve course_id and names
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

        @self.tree.command(name="server_status", description="View the current Canvas configuration for this server")
        async def server_status(interaction: discord.Interaction):
            if not interaction.guild_id:
                await interaction.response.send_message("❌ This command must be run inside a Discord server.", ephemeral=True)
                return

            cfg = self.db.get_server_config(interaction.guild_id)
            if not cfg:
                await interaction.response.send_message(
                    "ℹ️ This server has not been configured yet. An admin can run `/server_setup`.",
                    ephemeral=True,
                )
                return

            section_id = cfg["section_id"]
            donors = self.db.get_section_donors(section_id)

            embed = discord.Embed(
                title="⚙️ Server Canvas Status",
                color=discord.Color.blue(),
            )
            embed.add_field(name="Section ID", value=f"`{section_id}` ({cfg.get('section_name') or 'N/A'})", inline=True)
            embed.add_field(name="Course", value=cfg.get("course_name") or "N/A", inline=True)
            embed.add_field(name="Announcements Channel", value=f"<#{cfg['announcements_channel_id']}>", inline=True)
            embed.add_field(name="Assignments Channel", value=f"<#{cfg['assignments_channel_id']}>", inline=True)
            embed.add_field(name="Active Donor Accounts", value=f"**{len(donors)}** active donors in rotation", inline=True)
            embed.add_field(name="Last Donor Index", value=f"`{cfg.get('last_donor_index', 0)}`", inline=True)

            await interaction.response.send_message(embed=embed)

        @self.tree.command(name="server_unlink", description="Reset Canvas settings for this server (Admin only)")
        @app_commands.default_permissions(manage_guild=True)
        async def server_unlink(interaction: discord.Interaction):
            if not interaction.guild_id:
                await interaction.response.send_message("❌ This command must be run inside a Discord server.", ephemeral=True)
                return

            removed = self.db.delete_server_config(interaction.guild_id)
            if removed:
                await interaction.response.send_message("🗑️ Server Canvas configuration removed successfully.", ephemeral=True)
            else:
                await interaction.response.send_message("ℹ️ No configuration found for this server.", ephemeral=True)

        @self.tree.command(name="sync_now", description="Manually trigger an immediate sync of announcements & assignments")
        async def sync_now(interaction: discord.Interaction):
            await interaction.response.defer(ephemeral=True, thinking=True)
            counts = await self.sync_canvas_data()
            await interaction.followup.send(
                f"✅ **Sync Complete**:\n"
                f"• User Announcement DMs: **{counts['user_dms']}**\n"
                f"• Server Announcements: **{counts['server_announcements']}**\n"
                f"• Server Assignments: **{counts['server_assignments']}**\n"
                f"• 12h Deadline Reminders: **{counts['reminders']}**",
                ephemeral=True,
            )

    # --- Canvas Sync Engine ---

    async def sync_canvas_data(self) -> Dict[str, int]:
        """
        Executes one full sync pass:
        1. Checks servers using rotating donor accounts ("changes every time").
        2. Checks distinct courses and DMs enrolled users when new announcements appear.
        3. Caches assignments and sends 12-hour deadline reminder DMs to uncompleted students.
        """
        counts = {"user_dms": 0, "server_announcements": 0, "server_assignments": 0, "reminders": 0}

        # Batch accumulators — flushed to DB in one transaction at the end
        pending_user_ann: List[Tuple[int, str]] = []
        pending_server_ann: List[Tuple[int, str]] = []
        pending_server_ass: List[Tuple[int, str]] = []
        pending_reminders: List[Tuple[int, str]] = []

        # Local cache for enrolled students per course (avoids duplicate DB queries)
        enrollment_cache: Dict[int, List[int]] = {}

        # 1. Sync Servers via Donor Rotation
        server_configs = self.db.get_all_server_configs()
        course_announcements_pool: Dict[int, List[Dict[str, Any]]] = {}

        for cfg in server_configs:
            guild_id = cfg["guild_id"]
            section_id = cfg["section_id"]

            # Select rotating donor account ("changes every time")
            donor = self.db.get_next_donor_for_server(guild_id, section_id)
            if not donor:
                continue

            course_id = donor.get("course_id") or cfg.get("course_id")
            if not course_id:
                continue

            token = donor["canvas_token"]
            url = donor["canvas_url"]

            # A. Server Announcements
            try:
                announcements = await async_fetch_announcements(url, token, [course_id])
                course_announcements_pool[course_id] = announcements
                ann_channel = self.get_channel(cfg["announcements_channel_id"])
                if ann_channel:
                    for ann in announcements:
                        ann_id = ann["id"]
                        if (guild_id, ann_id) not in self.server_announcements_cache:
                            embed = discord.Embed(
                                title=f"📢 {ann['title']}",
                                url=ann.get("url") or None,
                                description=ann.get("message_clean") or "No content preview available.",
                                color=discord.Color.gold(),
                            )
                            embed.add_field(name="Course", value=cfg.get("course_name") or f"Course {course_id}", inline=True)
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
                print(f"[Bot] Error syncing announcements for server {guild_id} with donor {donor['discord_id']}: {e}")

            # B. Server Assignments
            try:
                assignments = await async_fetch_assignments(url, token, course_id)
                self.cached_assignments_pool[course_id] = assignments
                ass_channel = self.get_channel(cfg["assignments_channel_id"])
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
                            embed.add_field(name="Course", value=cfg.get("course_name") or f"Course {course_id}", inline=True)
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
                print(f"[Bot] Error syncing assignments for server {guild_id} with donor {donor['discord_id']}: {e}")

        # 2. Sync User Announcements (DMs to all enrolled students)
        distinct_course_ids = self.db.get_all_distinct_course_ids()

        # Pre-fetch announcements for courses not already in pool (concurrently)
        missing_ann_courses = [cid for cid in distinct_course_ids if cid not in course_announcements_pool]
        if missing_ann_courses:
            donors_for_ann = {cid: self.db.get_any_active_token_for_course(cid) for cid in missing_ann_courses}

            async def _fetch_ann(cid, donor):
                try:
                    return cid, await async_fetch_announcements(donor["canvas_url"], donor["canvas_token"], [cid])
                except Exception as e:
                    print(f"[Bot] Error fetching announcements for course {cid}: {e}")
                    return cid, None

            ann_tasks = [_fetch_ann(cid, d) for cid, d in donors_for_ann.items() if d]
            for cid, result in await asyncio.gather(*ann_tasks):
                if result is not None:
                    course_announcements_pool[cid] = result

        for course_id in distinct_course_ids:
            announcements = course_announcements_pool.get(course_id)

            if not announcements:
                continue

            enrolled_students = enrollment_cache.get(course_id)
            if enrolled_students is None:
                enrolled_students = self.db.get_users_enrolled_in_course(course_id)
                enrollment_cache[course_id] = enrolled_students
            for ann in announcements:
                ann_id = ann["id"]
                for student_id in enrolled_students:
                    if (student_id, ann_id) in self.user_announcements_cache:
                        continue

                    # Dispatch DM to student
                    try:
                        user = self.get_user(student_id) or await self.fetch_user(student_id)
                        if user:
                            embed = discord.Embed(
                                title=f"📢 New Course Announcement: {ann['title']}",
                                url=ann.get("url") or None,
                                description=ann.get("message_clean") or "No content preview available.",
                                color=discord.Color.gold(),
                            )
                            embed.add_field(name="Author", value=ann.get("author", "Instructor"), inline=True)
                            if ann.get("posted_at"):
                                embed.set_footer(text=f"Posted: {ann['posted_at']}")

                            await user.send(embed=embed)
                            self.user_announcements_cache.add((student_id, ann_id))
                            pending_user_ann.append((student_id, ann_id))
                            counts["user_dms"] += 1
                    except discord.Forbidden:
                        # User has DMs closed; still mark delivered so we don't retry endlessly
                        self.user_announcements_cache.add((student_id, ann_id))
                        pending_user_ann.append((student_id, ann_id))
                    except Exception as e:
                        print(f"[Bot] Failed to send DM to student {student_id}: {e}")

        # 3. 12-Hour Deadline Reminders (Cache assignments & check student completion)
        now = datetime.now(timezone.utc)

        # Pre-fetch assignments for courses not already cached (concurrently)
        missing_ass_courses = [cid for cid in distinct_course_ids if cid not in self.cached_assignments_pool]
        if missing_ass_courses:
            donors_for_ass = {cid: self.db.get_any_active_token_for_course(cid) for cid in missing_ass_courses}

            async def _fetch_ass(cid, donor):
                try:
                    return cid, await async_fetch_assignments(donor["canvas_url"], donor["canvas_token"], cid)
                except Exception as e:
                    print(f"[Bot] Error caching assignments for course {cid}: {e}")
                    return cid, None

            ass_tasks = [_fetch_ass(cid, d) for cid, d in donors_for_ass.items() if d]
            for cid, result in await asyncio.gather(*ass_tasks):
                if result is not None:
                    self.cached_assignments_pool[cid] = result

        for course_id in distinct_course_ids:

            assignments = self.cached_assignments_pool.get(course_id, [])
            enrolled_student_ids = enrollment_cache.get(course_id)
            if enrolled_student_ids is None:
                enrolled_student_ids = self.db.get_users_enrolled_in_course(course_id)
                enrollment_cache[course_id] = enrolled_student_ids
            if not enrolled_student_ids or not assignments:
                continue

            for a in assignments:
                due_at_str = a.get("due_at")
                if not due_at_str:
                    continue

                due_dt = parse_canvas_date(due_at_str)
                if not due_dt:
                    continue

                time_left = (due_dt - now).total_seconds()
                # Check if deadline is within 12 hours (and has not passed yet)
                if not (0 < time_left <= 12 * 3600):
                    continue

                ass_id = str(a["id"])
                for student_id in enrolled_student_ids:
                    if (student_id, ass_id) in self.user_reminders_cache:
                        continue

                    student = self.db.get_user(student_id)
                    if not student or not student.get("is_active"):
                        continue

                    # Check if student already completed this assignment
                    try:
                        is_done = await async_check_user_assignment_completion(
                            student["canvas_url"], student["canvas_token"], course_id, int(ass_id)
                        )
                    except Exception as e:
                        print(f"[Bot] Error checking completion for student {student_id}, task {ass_id}: {e}")
                        continue

                    if is_done:
                        # Completed or locked; record reminder so we do not re-check
                        self.user_reminders_cache.add((student_id, ass_id))
                        pending_reminders.append((student_id, ass_id))
                        continue

                    # Student has not completed it: send 12-hour reminder DM
                    try:
                        user_obj = self.get_user(student_id) or await self.fetch_user(student_id)
                        if user_obj:
                            ts = int(due_dt.timestamp())
                            embed = discord.Embed(
                                title=f"⏰ 12-Hour Deadline Reminder: {a['name']}",
                                url=a.get("url") or None,
                                description=(
                                    f"⚠️ Your assignment **{a['name']}** is due in less than 12 hours!\n\n"
                                    f"⏰ **Due Date:** <t:{ts}:F> (<t:{ts}:R>)\n"
                                    f"Please ensure your submission is turned in on time."
                                ),
                                color=discord.Color.red(),
                            )
                            enrollments = self.db.get_user_enrollments(student_id)
                            for e in enrollments:
                                if e["course_id"] == course_id:
                                    embed.add_field(name="Course", value=e.get("course_name", f"Course {course_id}"), inline=True)
                                    break
                            if a.get("points_possible") is not None:
                                embed.add_field(name="Points", value=f"{a['points_possible']} pts", inline=True)
                            embed.set_footer(text="Automated Canvas Deadline Reminder")

                            await user_obj.send(embed=embed)
                            counts["reminders"] += 1
                    except discord.Forbidden:
                        pass
                    except Exception as e:
                        print(f"[Bot] Failed to send reminder DM to {student_id}: {e}")

                    self.user_reminders_cache.add((student_id, ass_id))
                    pending_reminders.append((student_id, ass_id))

        # Flush all pending deliveries to DB in one transaction
        self.db.flush_deliveries(pending_user_ann, pending_server_ann, pending_server_ass, pending_reminders)
        return counts

    @tasks.loop(minutes=5)
    async def scan_canvas_loop(self):
        await self.wait_until_ready()
        try:
            print("[Scan] Starting Canvas sync pass...")
            counts = await self.sync_canvas_data()
            print(f"[Scan] Completed sync pass: {counts}")
        except Exception as e:
            print(f"[Scan] Exception during sync loop: {e}")

    @scan_canvas_loop.before_loop
    async def before_scan_loop(self):
        await self.wait_until_ready()


def run():
    token = BOT_TOKEN
    if not token:
        raise ValueError("BOT_TOKEN is not configured. Please set BOT_TOKEN in .env.")
    bot = CanvasDiscordBot()
    bot.run(token)


if __name__ == "__main__":
    run()
