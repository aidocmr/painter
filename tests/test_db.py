import pytest
import tempfile
import os
from painterv3.db import Database


@pytest.fixture
def db():
    fd, path = tempfile.mkstemp(suffix=".db")
    os.close(fd)
    database = Database(db_path=path)
    yield database
    if os.path.exists(path):
        os.remove(path)


def test_user_lifecycle_and_enrollments(db):
    enrollments = [
        {"course_id": 101, "section_id": 201, "course_name": "CS 101", "section_name": "S1"},
        {"course_id": 102, "section_id": 202, "course_name": "Math 101", "section_name": "S2"},
    ]
    db.save_user(
        discord_id=12345,
        canvas_token="mock_token_1",
        canvas_url="https://canvas.test",
        canvas_user_id=999,
        canvas_name="Alice",
        enrollments=enrollments,
    )

    user = db.get_user(12345)
    assert user is not None
    assert user["canvas_name"] == "Alice"
    assert user["canvas_token"] == "mock_token_1"
    assert user["is_active"] == 1

    stored_enrollments = db.get_user_enrollments(12345)
    assert len(stored_enrollments) == 2
    assert stored_enrollments[0]["course_id"] in (101, 102)

    # Test course enrollment lookup
    students_in_101 = db.get_users_enrolled_in_course(101)
    assert students_in_101 == [12345]

    # Test update / deactivate
    db.set_user_active(12345, False)
    assert db.get_user(12345)["is_active"] == 0
    assert db.get_users_enrolled_in_course(101) == []

    # Test delete
    assert db.delete_user(12345) is True
    assert db.get_user(12345) is None
    assert db.get_user_enrollments(12345) == []


def test_server_config_and_donor_rotation(db):
    # Setup server config
    db.save_server_config(
        guild_id=777,
        section_id=500,
        course_id=100,
        course_name="Software Engineering",
        section_name="SEC-A",
        announcements_channel_id=1111,
        assignments_channel_id=2222,
    )

    cfg = db.get_server_config(777)
    assert cfg["section_id"] == 500
    assert cfg["announcements_channel_id"] == 1111

    # No donors yet
    assert db.get_next_donor_for_server(777, 500) is None

    # Add 3 donors to section 500
    for uid, name in [(1, "User1"), (2, "User2"), (3, "User3")]:
        db.save_user(
            discord_id=uid,
            canvas_token=f"tok_{uid}",
            canvas_url="https://canvas.test",
            canvas_user_id=uid * 10,
            canvas_name=name,
            enrollments=[{"course_id": 100, "section_id": 500, "course_name": "SE", "section_name": "SEC-A"}],
        )

    # Verify donor rotation changes every time
    d1 = db.get_next_donor_for_server(777, 500)
    d2 = db.get_next_donor_for_server(777, 500)
    d3 = db.get_next_donor_for_server(777, 500)
    d4 = db.get_next_donor_for_server(777, 500)

    assert d1["discord_id"] == 1
    assert d2["discord_id"] == 2
    assert d3["discord_id"] == 3
    assert d4["discord_id"] == 1  # Loops back to 1


def test_deduplication_and_cache(db):
    assert not db.is_announcement_delivered_to_user(123, "ann_1")
    db.record_user_announcement_delivery(123, "ann_1")
    assert db.is_announcement_delivered_to_user(123, "ann_1")

    assert not db.is_announcement_delivered_to_server(999, "ann_1")
    db.record_server_announcement_delivery(999, "ann_1")
    assert db.is_announcement_delivered_to_server(999, "ann_1")

    assert not db.is_assignment_delivered_to_server(999, "ass_1")
    db.record_server_assignment_delivery(999, "ass_1")
    assert db.is_assignment_delivered_to_server(999, "ass_1")

    assert not db.is_deadline_reminder_sent(123, "ass_1")
    db.record_deadline_reminder(123, "ass_1")
    assert db.is_deadline_reminder_sent(123, "ass_1")

    user_ann, srv_ann, srv_ass, user_rem = db.load_cache_sets()
    assert (123, "ann_1") in user_ann
    assert (999, "ann_1") in srv_ann
    assert (999, "ass_1") in srv_ass
    assert (123, "ass_1") in user_rem

