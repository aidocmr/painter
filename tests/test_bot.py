import asyncio
import os
import random
import tempfile
from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock, MagicMock, patch

import discord
import pytest

from painterv3.bot import CanvasDiscordBot, LoginModal


@pytest.fixture
def temp_db_path():
    fd, path = tempfile.mkstemp(suffix=".db")
    os.close(fd)
    yield path
    if os.path.exists(path):
        os.remove(path)


@pytest.mark.asyncio
async def test_bot_command_tree_registration(temp_db_path):
    bot = CanvasDiscordBot(db_path=temp_db_path)
    bot._register_commands()

    command_names = [cmd.name for cmd in bot.tree.get_commands()]
    expected_commands = [
        "login",
        "logout",
        "my_courses",
        "my_assignments",
        "todo",
        "deadlines",
        "server_setup",
        "section_setup",
        "section_list",
        "section_unlink",
        "set_command_channel",
        "set_school_announcements",
        "set_canvas_url",
        "server_status",
        "server_unlink",
        "leaderboard",
        "coins",
        "claim_coins",
        "flipcoin",
        "sync_now",
    ]
    for expected in expected_commands:
        assert expected in command_names, f"Expected slash command '{expected}' was not registered"


@pytest.mark.asyncio
async def test_dm_broadcasts_deprecated_by_default(temp_db_path):
    bot = CanvasDiscordBot(db_path=temp_db_path, enable_dm_broadcasts=False)
    db = bot.db

    db.save_user(
        discord_id=1001,
        canvas_token="tok_1",
        canvas_url="https://canvas.test",
        canvas_user_id=1,
        canvas_name="Alice",
        enrollments=[{"course_id": 99, "section_id": 500, "course_name": "CS 101"}],
    )
    mock_student = AsyncMock(spec=discord.User)
    bot.get_user = MagicMock(return_value=mock_student)

    sample_announcements = [{"id": "ann_1", "course_id": 99, "title": "Ann 1", "message_clean": "Msg"}]

    with patch("painterv3.bot.async_fetch_announcements", AsyncMock(return_value=sample_announcements)), \
         patch("painterv3.bot.async_fetch_assignments", AsyncMock(return_value=[])):
        counts = await bot.sync_canvas_data()
        # Direct DM broadcasts are deprecated & disabled by default
        assert counts["user_dms"] == 0
        mock_student.send.assert_not_called()


@pytest.mark.asyncio
async def test_sync_canvas_data_server_and_legacy_dms(temp_db_path):
    bot = CanvasDiscordBot(db_path=temp_db_path, enable_dm_broadcasts=True)
    db = bot.db

    # Seed 2 users in section 500
    db.save_user(
        discord_id=1001,
        canvas_token="tok_1",
        canvas_url="https://canvas.test",
        canvas_user_id=1,
        canvas_name="Alice",
        enrollments=[{"course_id": 99, "section_id": 500, "course_name": "CS 101", "section_name": "S1"}],
    )
    db.save_user(
        discord_id=1002,
        canvas_token="tok_2",
        canvas_url="https://canvas.test",
        canvas_user_id=2,
        canvas_name="Bob",
        enrollments=[{"course_id": 99, "section_id": 500, "course_name": "CS 101", "section_name": "S1"}],
    )

    # Seed server config
    db.save_server_config(
        guild_id=8888,
        section_id=500,
        course_id=99,
        course_name="CS 101",
        section_name="S1",
        announcements_channel_id=701,
        assignments_channel_id=702,
    )

    # Mock channels & user
    mock_ann_channel = AsyncMock(spec=discord.TextChannel)
    mock_ass_channel = AsyncMock(spec=discord.TextChannel)

    def get_channel_mock(cid):
        if cid == 701:
            return mock_ann_channel
        if cid == 702:
            return mock_ass_channel
        return None

    bot.get_channel = MagicMock(side_effect=get_channel_mock)

    mock_student = AsyncMock(spec=discord.User)
    bot.get_user = MagicMock(return_value=mock_student)

    sample_announcements = [
        {
            "id": "ann_42",
            "course_id": 99,
            "title": "Welcome to CS 101",
            "author": "Prof. Smith",
            "posted_at": "2026-09-27T10:00:00Z",
            "message_clean": "Class starts tomorrow!",
            "url": "https://canvas.test/ann/42",
        }
    ]
    sample_assignments = [
        {
            "id": "ass_101",
            "course_id": 99,
            "name": "Homework 1",
            "due_at": "2026-10-01T23:59:59Z",
            "points_possible": 50,
            "url": "https://canvas.test/ass/101",
            "description_clean": "Complete problem set.",
        }
    ]

    with patch("painterv3.bot.async_fetch_announcements", AsyncMock(return_value=sample_announcements)), \
         patch("painterv3.bot.async_fetch_assignments", AsyncMock(return_value=sample_assignments)):

        # First sync pass
        counts = await bot.sync_canvas_data()

        # Both enrolled students (Alice and Bob) should get a DM in legacy mode
        assert counts["user_dms"] == 2
        # Server should receive 1 announcement and 1 assignment
        assert counts["server_announcements"] == 1
        assert counts["server_assignments"] == 1

        assert mock_ann_channel.send.call_count == 1
        assert mock_ass_channel.send.call_count == 1
        assert mock_student.send.call_count == 2

        # Second sync pass: deduplication should prevent any re-sends
        counts_second = await bot.sync_canvas_data()
        assert counts_second["user_dms"] == 0
        assert counts_second["server_announcements"] == 0
        assert counts_second["server_assignments"] == 0


@pytest.mark.asyncio
async def test_donor_rotation_on_consecutive_syncs(temp_db_path):
    bot = CanvasDiscordBot(db_path=temp_db_path, enable_dm_broadcasts=False)
    db = bot.db

    # 3 donors in section 777
    for uid in (10, 20, 30):
        db.save_user(
            discord_id=uid,
            canvas_token=f"tok_{uid}",
            canvas_url="https://canvas.test",
            canvas_user_id=uid,
            canvas_name=f"User_{uid}",
            enrollments=[{"course_id": 55, "section_id": 777, "course_name": "Physics", "section_name": "Sec1"}],
        )

    db.save_server_config(
        guild_id=999,
        section_id=777,
        course_id=55,
        course_name="Physics",
        section_name="Sec1",
        announcements_channel_id=1,
        assignments_channel_id=2,
    )

    used_donors = []
    async def mock_fetch_ann(url, token, course_ids):
        used_donors.append(token)
        return []

    with patch("painterv3.bot.async_fetch_announcements", side_effect=mock_fetch_ann), \
         patch("painterv3.bot.async_fetch_assignments", AsyncMock(return_value=[])):

        await bot.sync_canvas_data()
        await bot.sync_canvas_data()
        await bot.sync_canvas_data()
        await bot.sync_canvas_data()

    server_sync_tokens = [tok for tok in used_donors if tok in ("tok_10", "tok_20", "tok_30")]
    assert len(server_sync_tokens) >= 4
    assert server_sync_tokens[0] == "tok_10"
    assert server_sync_tokens[1] == "tok_20"
    assert server_sync_tokens[2] == "tok_30"
    assert server_sync_tokens[3] == "tok_10"


@pytest.mark.asyncio
async def test_targeted_announcement_dms_by_course(temp_db_path):
    bot = CanvasDiscordBot(db_path=temp_db_path, enable_dm_broadcasts=True)
    db = bot.db

    db.save_user(1, "t1", "https://canvas.test", 1, "User1", [{"course_id": 10, "section_id": 101}])
    db.save_user(2, "t2", "https://canvas.test", 2, "User2", [{"course_id": 10, "section_id": 101}])
    db.save_user(3, "t3", "https://canvas.test", 3, "User3", [{"course_id": 20, "section_id": 201}])

    mock_users = {1: AsyncMock(spec=discord.User), 2: AsyncMock(spec=discord.User), 3: AsyncMock(spec=discord.User)}
    bot.get_user = MagicMock(side_effect=lambda uid: mock_users.get(uid))

    async def mock_fetch_ann(url, token, course_ids):
        cid = course_ids[0]
        if cid == 10:
            return [{"id": "ann_10", "course_id": 10, "title": "Math Ann", "message_clean": "HW 1"}]
        return []

    with patch("painterv3.bot.async_fetch_announcements", side_effect=mock_fetch_ann), \
         patch("painterv3.bot.async_fetch_assignments", AsyncMock(return_value=[])):

        counts = await bot.sync_canvas_data()
        assert counts["user_dms"] == 2

        assert mock_users[1].send.call_count == 1
        assert mock_users[2].send.call_count == 1
        assert mock_users[3].send.call_count == 0


@pytest.mark.asyncio
async def test_server_id_authorization_restriction(temp_db_path):
    # Only guild 12345 is authorized
    bot = CanvasDiscordBot(db_path=temp_db_path, allowed_guild_ids={12345})
    bot._register_commands()

    # Allowed guild
    assert bot._is_guild_authorized(12345) is True
    # Disallowed guild
    assert bot._is_guild_authorized(99999) is False

    # Simulate interaction check for disallowed guild
    mock_interaction = AsyncMock(spec=discord.Interaction)
    mock_interaction.guild_id = 99999
    mock_interaction.response = AsyncMock()
    mock_interaction.response.is_done = MagicMock(return_value=False)

    tree_check = bot.tree.interaction_check
    result = await tree_check(mock_interaction)
    assert result is False
    mock_interaction.response.send_message.assert_awaited_once()
    assert "Access Denied" in mock_interaction.response.send_message.call_args[0][0]


@pytest.mark.asyncio
async def test_dedicated_command_channel_restriction(temp_db_path):
    bot = CanvasDiscordBot(db_path=temp_db_path)
    bot._register_commands()
    db = bot.db

    # Configure command channel 888 for guild 777
    db.save_server_config(
        guild_id=777,
        section_id=None,
        course_id=None,
        course_name=None,
        section_name=None,
        announcements_channel_id=None,
        assignments_channel_id=None,
        command_channel_id=888,
    )

    mock_guild = MagicMock(spec=discord.Guild)
    mock_guild.id = 777
    mock_guild.text_channels = []

    mock_channel = MagicMock(spec=discord.TextChannel)
    mock_channel.id = 999  # NOT the command channel 888

    mock_user = MagicMock()
    mock_user.guild_permissions.administrator = False

    mock_cmd = MagicMock()
    mock_cmd.name = "deadlines"

    interaction = AsyncMock(spec=discord.Interaction)
    interaction.guild_id = 777
    interaction.guild = mock_guild
    interaction.channel = mock_channel
    interaction.channel_id = 999
    interaction.user = mock_user
    interaction.command = mock_cmd
    interaction.response = AsyncMock()
    interaction.response.is_done = MagicMock(return_value=False)

    tree_check = bot.tree.interaction_check
    result = await tree_check(interaction)
    assert result is False
    interaction.response.send_message.assert_awaited_once()
    assert "Channel Restricted" in interaction.response.send_message.call_args[0][0]


@pytest.mark.asyncio
async def test_multiple_sections_and_role_pings(temp_db_path):
    bot = CanvasDiscordBot(db_path=temp_db_path, enable_dm_broadcasts=False)
    db = bot.db

    # Create 2 donors: donor 1 for Section A (sec 101), donor 2 for Section B (sec 202)
    db.save_user(1, "tok_a", "https://canvas.test", 1, "Alice", [{"course_id": 50, "section_id": 101}])
    db.save_user(2, "tok_b", "https://canvas.test", 2, "Bob", [{"course_id": 60, "section_id": 202}])

    # Configure 2 sections with dedicated channels and roles
    db.save_server_section(
        guild_id=555,
        section_id=101,
        course_id=50,
        course_name="CS 101",
        section_name="Section A",
        channel_id=301,
        role_id=901,
    )
    db.save_server_section(
        guild_id=555,
        section_id=202,
        course_id=60,
        course_name="Math 101",
        section_name="Section B",
        channel_id=302,
        role_id=902,
    )

    channel_301 = AsyncMock(spec=discord.TextChannel)
    channel_302 = AsyncMock(spec=discord.TextChannel)

    def get_channel_mock(cid):
        if cid == 301:
            return channel_301
        if cid == 302:
            return channel_302
        return None

    bot.get_channel = MagicMock(side_effect=get_channel_mock)

    sample_sec_a_ann = [{"id": "ann_a1", "course_id": 50, "title": "Sec A HW Announced", "message_clean": "Do HW 1"}]
    sample_sec_b_ann = [{"id": "ann_b1", "course_id": 60, "title": "Sec B HW Announced", "message_clean": "Do HW 2"}]

    async def mock_fetch_ann(url, token, course_ids):
        if course_ids[0] == 50:
            return sample_sec_a_ann
        elif course_ids[0] == 60:
            return sample_sec_b_ann
        return []

    with patch("painterv3.bot.async_fetch_announcements", side_effect=mock_fetch_ann), \
         patch("painterv3.bot.async_fetch_assignments", AsyncMock(return_value=[])):

        counts = await bot.sync_canvas_data()
        assert counts["section_announcements"] == 2

        # Verify section A received announcement with role 901 ping
        channel_301.send.assert_awaited_once()
        assert "<@&901>" in channel_301.send.call_args[1]["content"]

        # Verify section B received announcement with role 902 ping
        channel_302.send.assert_awaited_once()
        assert "<@&902>" in channel_302.send.call_args[1]["content"]


@pytest.mark.asyncio
async def test_school_announcements_channel_and_role(temp_db_path):
    bot = CanvasDiscordBot(db_path=temp_db_path, enable_dm_broadcasts=False)
    db = bot.db

    db.save_user(1, "tok", "https://canvas.test", 1, "Alice", [{"course_id": 10, "section_id": 101}])
    db.save_server_config(
        guild_id=777,
        section_id=101,
        course_id=10,
        course_name="General Course",
        section_name="S1",
        announcements_channel_id=200,
        assignments_channel_id=201,
        school_announcements_channel_id=500,
        school_announcements_role_id=8888,
    )

    school_channel = AsyncMock(spec=discord.TextChannel)
    bot.get_channel = MagicMock(side_effect=lambda cid: school_channel if cid == 500 else None)

    global_ann = [
        {
            "id": "global_1",
            "course_id": 10,
            "title": "Campus Closure Tomorrow",
            "message_clean": "Heavy snow.",
            "is_section_specific": False,
        }
    ]

    with patch("painterv3.bot.async_fetch_announcements", AsyncMock(return_value=global_ann)), \
         patch("painterv3.bot.async_fetch_assignments", AsyncMock(return_value=[])):

        await bot.sync_canvas_data()
        school_channel.send.assert_awaited_once()
        assert "<@&8888>" in school_channel.send.call_args[1]["content"]


@pytest.mark.asyncio
async def test_deadlines_slash_command(temp_db_path):
    bot = CanvasDiscordBot(db_path=temp_db_path)
    bot._register_commands()
    db = bot.db

    db.save_user(
        discord_id=1000,
        canvas_token="tok_d",
        canvas_url="https://canvas.test",
        canvas_user_id=1,
        canvas_name="Deadline Student",
        enrollments=[{"course_id": 10, "section_id": 101, "course_name": "CS 101"}],
    )

    interaction = AsyncMock(spec=discord.Interaction)
    interaction.user = MagicMock()
    interaction.user.id = 1000
    interaction.response = AsyncMock()
    interaction.followup = AsyncMock()

    deadlines_cmd = bot.tree.get_command("deadlines")

    now = datetime.now(timezone.utc)
    d1 = (now + timedelta(days=2)).strftime("%Y-%m-%dT%H:%M:%SZ")
    d2 = (now + timedelta(days=5)).strftime("%Y-%m-%dT%H:%M:%SZ")

    mock_assignments = [
        {"id": "1", "course_id": 10, "name": "Task 1", "due_at": d1, "is_completed": False, "is_locked": False},
        {"id": "2", "course_id": 10, "name": "Task 2", "due_at": d2, "is_completed": False, "is_locked": False},
    ]

    with patch("painterv3.bot.async_fetch_assignments", AsyncMock(return_value=mock_assignments)):
        await deadlines_cmd.callback(interaction, days=7)

        interaction.followup.send.assert_awaited_once()
        embed = interaction.followup.send.call_args[1]["embed"]
        assert "Upcoming Deadlines" in embed.title
        assert len(embed.fields) == 2
        assert "Task 1" in embed.fields[0].name
        assert "Task 2" in embed.fields[1].name


@pytest.mark.asyncio
async def test_gamification_submission_verification_and_ranking(temp_db_path):
    bot = CanvasDiscordBot(db_path=temp_db_path)
    db = bot.db

    # 4 students
    for i in range(1, 5):
        db.save_user(
            discord_id=i,
            canvas_token=f"tok_{i}",
            canvas_url="https://canvas.test",
            canvas_user_id=i,
            canvas_name=f"Student {i}",
            enrollments=[{"course_id": 10, "section_id": 100}],
        )

    # Award submissions with increasing timestamps
    # 1st: 100 coins
    awarded1, rank1, pts1 = db.record_submission_and_award_points(
        "ass_1", 1, 10, "2026-10-10T01:00:00Z"
    )
    assert awarded1 is True
    assert rank1 == 1
    assert pts1 == 100

    # 2nd: 75 coins
    awarded2, rank2, pts2 = db.record_submission_and_award_points(
        "ass_1", 2, 10, "2026-10-10T02:00:00Z"
    )
    assert awarded2 is True
    assert rank2 == 2
    assert pts2 == 75

    # 3rd: 50 coins
    awarded3, rank3, pts3 = db.record_submission_and_award_points(
        "ass_1", 3, 10, "2026-10-10T03:00:00Z"
    )
    assert awarded3 is True
    assert rank3 == 3
    assert pts3 == 50

    # 4th: 25 coins
    awarded4, rank4, pts4 = db.record_submission_and_award_points(
        "ass_1", 4, 10, "2026-10-10T04:00:00Z"
    )
    assert awarded4 is True
    assert rank4 == 4
    assert pts4 == 25

    # Duplicate submission cannot be awarded again
    awarded_dup, _, _ = db.record_submission_and_award_points(
        "ass_1", 1, 10, "2026-10-10T01:00:00Z"
    )
    assert awarded_dup is False

    # Check leaderboard reflects rankings
    leaders = db.get_top_leaderboard(10)
    assert len(leaders) == 4
    assert leaders[0]["discord_id"] == 1
    assert leaders[0]["balance"] == 100
    assert leaders[1]["discord_id"] == 2
    assert leaders[1]["balance"] == 75
    assert leaders[2]["discord_id"] == 3
    assert leaders[2]["balance"] == 50
    assert leaders[3]["discord_id"] == 4
    assert leaders[3]["balance"] == 25


@pytest.mark.asyncio
async def test_leaderboard_slash_command(temp_db_path):
    bot = CanvasDiscordBot(db_path=temp_db_path)
    bot._register_commands()
    db = bot.db

    db.update_user_coins(1001, 200)

    interaction = AsyncMock(spec=discord.Interaction)
    interaction.response = AsyncMock()

    leaderboard_cmd = bot.tree.get_command("leaderboard")
    await leaderboard_cmd.callback(interaction)

    interaction.response.send_message.assert_awaited_once()
    assert interaction.response.send_message.call_args[1]["ephemeral"] is False
    embed = interaction.response.send_message.call_args[1]["embed"]
    assert "Canvas Activity Leaderboard" in embed.title


@pytest.mark.asyncio
async def test_flipcoin_slash_command(temp_db_path):
    bot = CanvasDiscordBot(db_path=temp_db_path)
    bot._register_commands()
    db = bot.db

    # User starts with 50 coins
    db.update_user_coins(9999, 50)

    interaction = AsyncMock(spec=discord.Interaction)
    interaction.user = MagicMock()
    interaction.user.id = 9999
    interaction.response = AsyncMock()

    flipcoin_cmd = bot.tree.get_command("flipcoin")

    # 1. Bet > balance
    choice_mock = MagicMock()
    choice_mock.value = "heads"
    await flipcoin_cmd.callback(interaction, choice=choice_mock, bet=100)
    assert "Insufficient Coins" in interaction.response.send_message.call_args[0][0]

    # 2. Bet <= 0
    interaction.response.send_message.reset_mock()
    await flipcoin_cmd.callback(interaction, choice=choice_mock, bet=0)
    assert "Bet must be at least 1 coin" in interaction.response.send_message.call_args[0][0]

    # 3. Winning flip
    interaction.response.send_message.reset_mock()
    with patch("random.choice", return_value="heads"):
        await flipcoin_cmd.callback(interaction, choice=choice_mock, bet=20)
        embed = interaction.response.send_message.call_args[1]["embed"]
        assert "You Won!" in embed.title
        assert db.get_user_economy(9999)["balance"] == 70

    # 4. Losing flip
    interaction.response.send_message.reset_mock()
    with patch("random.choice", return_value="tails"):
        await flipcoin_cmd.callback(interaction, choice=choice_mock, bet=20)
        embed = interaction.response.send_message.call_args[1]["embed"]
        assert "You Lost!" in embed.title
        assert db.get_user_economy(9999)["balance"] == 50


@pytest.mark.asyncio
async def test_12h_deadline_reminders(temp_db_path):
    bot = CanvasDiscordBot(db_path=temp_db_path, enable_dm_broadcasts=True)
    db = bot.db

    # Create 2 users: User 1 (not completed), User 2 (completed)
    db.save_user(
        discord_id=7001,
        canvas_token="tok_u1",
        canvas_url="https://canvas.test",
        canvas_user_id=1,
        canvas_name="Student One",
        enrollments=[{"course_id": 55, "section_id": 551, "course_name": "Math 101"}],
    )
    db.save_user(
        discord_id=7002,
        canvas_token="tok_u2",
        canvas_url="https://canvas.test",
        canvas_user_id=2,
        canvas_name="Student Two",
        enrollments=[{"course_id": 55, "section_id": 551, "course_name": "Math 101"}],
    )

    now = datetime.now(timezone.utc)
    due_in_6h = (now + timedelta(hours=6)).strftime("%Y-%m-%dT%H:%M:%SZ")

    mock_assignments = [
        {
            "id": 9999,
            "name": "Homework 1",
            "due_at": due_in_6h,
            "points_possible": 100,
            "url": "https://canvas.test/courses/55/assignments/9999",
            "course_id": 55,
        }
    ]

    mock_user_1 = AsyncMock()
    mock_user_1.send = AsyncMock()
    mock_user_2 = AsyncMock()
    mock_user_2.send = AsyncMock()

    async def fake_fetch_user(user_id):
        if user_id == 7001:
            return mock_user_1
        elif user_id == 7002:
            return mock_user_2
        return None

    bot.fetch_user = fake_fetch_user
    bot.get_user = lambda uid: None

    async def fake_check_completion(url, token, course_id, assignment_id):
        if token == "tok_u1":
            return False
        return True

    with patch("painterv3.bot.async_fetch_assignments", AsyncMock(return_value=mock_assignments)):
        with patch("painterv3.bot.async_check_user_assignment_completion", side_effect=fake_check_completion):
            counts = await bot.sync_canvas_data()

    assert counts["reminders"] == 1
    mock_user_1.send.assert_awaited_once()
    mock_user_2.send.assert_not_awaited()

    assert db.is_deadline_reminder_sent(7001, "9999")
    assert db.is_deadline_reminder_sent(7002, "9999")

    mock_user_1.send.reset_mock()
    with patch("painterv3.bot.async_fetch_assignments", AsyncMock(return_value=mock_assignments)):
        with patch("painterv3.bot.async_check_user_assignment_completion", side_effect=fake_check_completion):
            counts2 = await bot.sync_canvas_data()

    assert counts2["reminders"] == 0
    mock_user_1.send.assert_not_awaited()
