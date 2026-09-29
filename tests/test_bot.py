import pytest
import os
import tempfile
from datetime import datetime, timezone, timedelta
from unittest.mock import AsyncMock, MagicMock, patch
import discord

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
        "server_setup",
        "server_status",
        "server_unlink",
        "sync_now",
    ]
    for expected in expected_commands:
        assert expected in command_names, f"Expected slash command '{expected}' was not registered"


@pytest.mark.asyncio
async def test_sync_canvas_data_server_and_dms(temp_db_path):
    bot = CanvasDiscordBot(db_path=temp_db_path)
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

    # Mock canvas fetching
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

        # Both enrolled students (Alice and Bob) should get a DM
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
    bot = CanvasDiscordBot(db_path=temp_db_path)
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

    # The server sync used rotating donor tokens
    # Filter the tokens recorded from server sync
    server_sync_tokens = [tok for tok in used_donors if tok in ("tok_10", "tok_20", "tok_30")]
    assert len(server_sync_tokens) >= 4
    assert server_sync_tokens[0] == "tok_10"
    assert server_sync_tokens[1] == "tok_20"
    assert server_sync_tokens[2] == "tok_30"
    assert server_sync_tokens[3] == "tok_10"  # Rotates back!


@pytest.mark.asyncio
async def test_targeted_announcement_dms_by_course(temp_db_path):
    bot = CanvasDiscordBot(db_path=temp_db_path)
    db = bot.db

    # User 1 & 2 in Course 10; User 3 in Course 20
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

        # Users 1 and 2 received DM; User 3 did NOT receive DM
        assert mock_users[1].send.call_count == 1
        assert mock_users[2].send.call_count == 1
        assert mock_users[3].send.call_count == 0


@pytest.mark.asyncio
async def test_login_modal_failure_handling(temp_db_path):
    bot = CanvasDiscordBot(db_path=temp_db_path)
    modal = LoginModal(bot)
    modal.canvas_token._value = "invalid_token_123"
    modal.canvas_url._value = "https://bad.url"

    interaction = AsyncMock(spec=discord.Interaction)
    interaction.response = AsyncMock()
    interaction.followup = AsyncMock()

    with patch("painterv3.bot.async_validate_and_get_profile", side_effect=Exception("Invalid access token")):
        await modal.on_submit(interaction)

        interaction.response.defer.assert_awaited_once_with(ephemeral=True, thinking=True)
        interaction.followup.send.assert_awaited_once()
        sent_msg = interaction.followup.send.call_args[0][0]
        assert "Canvas Login Failed" in sent_msg


@pytest.mark.asyncio
async def test_my_assignments_slash_command(temp_db_path):
    bot = CanvasDiscordBot(db_path=temp_db_path)
    bot._register_commands()
    db = bot.db

    db.save_user(
        discord_id=5000,
        canvas_token="tok_test",
        canvas_url="https://canvas.test",
        canvas_user_id=1,
        canvas_name="Test Student",
        enrollments=[{"course_id": 10, "section_id": 101, "course_name": "CS 101"}],
    )

    interaction = AsyncMock(spec=discord.Interaction)
    interaction.user = MagicMock()
    interaction.user.id = 5000
    interaction.response = AsyncMock()
    interaction.followup = AsyncMock()

    my_assignments_cmd = bot.tree.get_command("my_assignments")

    mock_uncompleted_hw = [
        {
            "id": "hw_99",
            "course_id": 10,
            "name": "Unsubmitted Project",
            "due_at": "2026-10-15T23:59:59Z",
            "points_possible": 100,
            "url": "http://canvas/hw99",
            "is_completed": False,
        }
    ]

    with patch("painterv3.bot.async_fetch_assignments", AsyncMock(return_value=mock_uncompleted_hw)) as mock_fetch:
        # Default run: uncompleted, upcoming with due dates
        await my_assignments_cmd.callback(interaction)

        mock_fetch.assert_awaited_once_with(
            "https://canvas.test", "tok_test", 10, uncompleted_only=False
        )
        interaction.followup.send.assert_awaited_once()
        embed = interaction.followup.send.call_args[1]["embed"]
        assert "Canvas Assignments" in embed.title
        assert "Unsubmitted Project" in embed.fields[0].name


@pytest.mark.asyncio
async def test_my_assignments_filters(temp_db_path):
    bot = CanvasDiscordBot(db_path=temp_db_path)
    bot._register_commands()
    db = bot.db

    db.save_user(
        discord_id=7000,
        canvas_token="tok_filter",
        canvas_url="https://canvas.test",
        canvas_user_id=7,
        canvas_name="Filter Student",
        enrollments=[
            {"course_id": 10, "section_id": 101, "course_name": "CS 10"},
            {"course_id": 20, "section_id": 201, "course_name": "Math 20"},
        ],
    )

    interaction = AsyncMock(spec=discord.Interaction)
    interaction.user = MagicMock()
    interaction.user.id = 7000
    interaction.response = AsyncMock()
    interaction.followup = AsyncMock()

    my_assignments_cmd = bot.tree.get_command("my_assignments")

    mock_pool = [
        # Course 10, upcoming, uncompleted
        {"id": "1", "course_id": 10, "name": "Upcoming CS HW", "due_at": "2099-10-01T00:00:00Z", "is_completed": False, "is_locked": False},
        # Course 10, undated, uncompleted
        {"id": "2", "course_id": 10, "name": "Undated CS HW", "due_at": None, "is_completed": False, "is_locked": False},
        # Course 10, past due, uncompleted
        {"id": "3", "course_id": 10, "name": "Overdue CS HW", "due_at": "2020-01-01T00:00:00Z", "is_completed": False, "is_locked": False},
        # Course 10, completed
        {"id": "4", "course_id": 10, "name": "Done CS HW", "due_at": "2020-01-01T00:00:00Z", "is_completed": True, "is_locked": False},
        # Course 20, upcoming, uncompleted
        {"id": "5", "course_id": 20, "name": "Upcoming Math HW", "due_at": "2099-10-01T00:00:00Z", "is_completed": False, "is_locked": False},
    ]

    with patch("painterv3.bot.async_fetch_assignments", AsyncMock(return_value=mock_pool)):
        # 1. Default: only upcoming uncompleted with due dates
        await my_assignments_cmd.callback(interaction)
        embed = interaction.followup.send.call_args[1]["embed"]
        hw_names = [f.name for f in embed.fields]
        assert "Upcoming CS HW" in hw_names
        assert "Upcoming Math HW" in hw_names
        assert "Undated CS HW" not in hw_names
        assert "Overdue CS HW" not in hw_names
        assert "Done CS HW" not in hw_names

        # 2. Filter: include_undated=True
        interaction.followup.send.reset_mock()
        await my_assignments_cmd.callback(interaction, include_undated=True)
        embed = interaction.followup.send.call_args[1]["embed"]
        hw_names = [f.name for f in embed.fields]
        assert "Undated CS HW" in hw_names

        # 3. Filter: past_due=True
        interaction.followup.send.reset_mock()
        await my_assignments_cmd.callback(interaction, past_due=True)
        embed = interaction.followup.send.call_args[1]["embed"]
        hw_names = [f.name for f in embed.fields]
        assert "Overdue CS HW" in hw_names
        assert "Upcoming CS HW" not in hw_names

        # 4. Filter: completed_only=True
        interaction.followup.send.reset_mock()
        await my_assignments_cmd.callback(interaction, completed_only=True, past_due=True)
        embed = interaction.followup.send.call_args[1]["embed"]
        hw_names = [f.name for f in embed.fields]
        assert "Done CS HW" in hw_names

        # 5. Filter: course="10"
        interaction.followup.send.reset_mock()
        await my_assignments_cmd.callback(interaction, course="10")
        embed = interaction.followup.send.call_args[1]["embed"]
        hw_names = [f.name for f in embed.fields]
        assert "Upcoming CS HW" in hw_names
        assert "Upcoming Math HW" not in hw_names


@pytest.mark.asyncio
async def test_my_todo_slash_command(temp_db_path):
    bot = CanvasDiscordBot(db_path=temp_db_path)
    bot._register_commands()
    db = bot.db

    db.save_user(
        discord_id=6000,
        canvas_token="tok_todo",
        canvas_url="https://canvas.test",
        canvas_user_id=1,
        canvas_name="Todo Student",
        enrollments=[{"course_id": 10, "section_id": 101, "course_name": "CS 101"}],
    )

    interaction = AsyncMock(spec=discord.Interaction)
    interaction.user = MagicMock()
    interaction.user.id = 6000
    interaction.response = AsyncMock()
    interaction.followup = AsyncMock()

    todo_cmd = bot.tree.get_command("todo")

    mock_todos = [
        {
            "id": "todo_101",
            "name": "Midterm Review Quiz",
            "due_at": "2026-10-20T23:59:59Z",
            "points_possible": 25,
            "url": "http://canvas/todo101",
            "course_id": 10,
            "type": "submitting",
        }
    ]

    with patch("painterv3.bot.async_fetch_todo_items", AsyncMock(return_value=mock_todos)) as mock_fetch:
        await todo_cmd.callback(interaction)

        mock_fetch.assert_awaited_once_with("https://canvas.test", "tok_todo")
        interaction.followup.send.assert_awaited_once()
        embed = interaction.followup.send.call_args[1]["embed"]
        assert "Canvas To-Do List" in embed.title
        assert "Midterm Review Quiz" in embed.fields[0].name


@pytest.mark.asyncio
async def test_12h_deadline_reminders(temp_db_path):
    bot = CanvasDiscordBot(db_path=temp_db_path)
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

    # Assignment due in 6 hours
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

    # Student 7001 has NOT completed (returns False), Student 7002 HAS completed (returns True)
    async def fake_check_completion(url, token, course_id, assignment_id):
        if token == "tok_u1":
            return False
        return True

    with patch("painterv3.bot.async_fetch_assignments", AsyncMock(return_value=mock_assignments)):
        with patch("painterv3.bot.async_check_user_assignment_completion", side_effect=fake_check_completion):
            counts = await bot.sync_canvas_data()

    # User 1 should have received 1 reminder DM
    assert counts["reminders"] == 1
    mock_user_1.send.assert_awaited_once()
    mock_user_2.send.assert_not_awaited()

    # DB should have recorded reminder for user 7001
    assert db.is_deadline_reminder_sent(7001, "9999")
    # User 7002 was marked completed so reminder also marked sent to prevent re-checking
    assert db.is_deadline_reminder_sent(7002, "9999")

    # Second sync pass should NOT send duplicate reminder
    mock_user_1.send.reset_mock()
    with patch("painterv3.bot.async_fetch_assignments", AsyncMock(return_value=mock_assignments)):
        with patch("painterv3.bot.async_check_user_assignment_completion", side_effect=fake_check_completion):
            counts2 = await bot.sync_canvas_data()

    assert counts2["reminders"] == 0
    mock_user_1.send.assert_not_awaited()




