import pytest
from unittest.mock import MagicMock, patch
from painterv3.canvas_client import clean_html, CanvasClient


def test_clean_html():
    raw = "<p>Hello <b>World</b>!</p><p>Check this <a href='https://example.com'>link</a>.</p><ul><li>Item 1</li><li>Item 2</li></ul>&amp; test"
    cleaned = clean_html(raw)
    assert "**World**" in cleaned
    assert "[link](https://example.com)" in cleaned
    assert "• Item 1" in cleaned
    assert "& test" in cleaned
    assert "<p>" not in cleaned
    assert "<ul>" not in cleaned


def test_clean_html_truncation():
    long_text = "<p>" + "A" * 2000 + "</p>"
    cleaned = clean_html(long_text, max_length=100)
    assert len(cleaned) == 100
    assert cleaned.endswith("...")


def test_canvas_client_validate_profile_mocked():
    with patch("painterv3.canvas_client.Canvas") as mock_canvas_cls:
        mock_instance = MagicMock()
        mock_canvas_cls.return_value = mock_instance

        mock_user = MagicMock()
        mock_user.id = 42
        mock_user.name = "John Doe"
        mock_instance.get_current_user.return_value = mock_user

        mock_course = MagicMock()
        mock_course.id = 101
        mock_course.name = "Intro to CS"
        mock_course.sections = [{"id": 555, "name": "CS-1A"}]
        mock_user.get_courses.return_value = [mock_course]

        client = CanvasClient("https://canvas.test", "fake_token")
        user_info, enrollments = client.validate_and_get_profile()

        assert user_info["id"] == 42
        assert user_info["name"] == "John Doe"
        assert len(enrollments) == 1
        assert enrollments[0]["course_id"] == 101
        assert enrollments[0]["section_id"] == 555
        assert enrollments[0]["course_name"] == "Intro to CS"


def test_is_submission_completed():
    from painterv3.canvas_client import is_submission_completed

    assert not is_submission_completed(None)
    assert not is_submission_completed({})
    assert not is_submission_completed({"workflow_state": "unsubmitted", "submitted_at": None})

    # Completed variations
    assert is_submission_completed({"submitted_at": "2026-09-27T10:00:00Z"})
    assert is_submission_completed({"workflow_state": "submitted"})
    assert is_submission_completed({"workflow_state": "graded"})
    assert is_submission_completed({"workflow_state": "pending_review"})
    assert is_submission_completed({"excused": True})
    assert is_submission_completed({"score": 90})


def test_fetch_course_assignments_uncompleted_filtering():
    with patch("painterv3.canvas_client.Canvas") as mock_canvas_cls:
        mock_instance = MagicMock()
        mock_canvas_cls.return_value = mock_instance

        mock_course = MagicMock()
        mock_instance.get_course.return_value = mock_course

        # Assignment 1: Completed
        a1 = MagicMock()
        a1.id = 1
        a1.name = "Done HW"
        a1.submission = {"workflow_state": "submitted", "submitted_at": "2026-09-01T00:00:00Z"}
        a1.due_at = "2026-09-05T00:00:00Z"
        a1.points_possible = 10
        a1.html_url = "http://hw1"
        a1.description = "Done"

        # Assignment 2: Uncompleted
        a2 = MagicMock()
        a2.id = 2
        a2.name = "Pending HW"
        a2.submission = {"workflow_state": "unsubmitted", "submitted_at": None}
        a2.due_at = "2026-10-01T00:00:00Z"
        a2.points_possible = 20
        a2.html_url = "http://hw2"
        a2.description = "Pending"

        mock_course.get_assignments.return_value = [a1, a2]

        client = CanvasClient("https://canvas.test", "tok")

        # When uncompleted_only=True, only a2 should be returned
        uncompleted = client.fetch_course_assignments(course_id=99, uncompleted_only=True)
        assert len(uncompleted) == 1
        assert uncompleted[0]["id"] == "2"
        assert uncompleted[0]["name"] == "Pending HW"

        # When uncompleted_only=False, both are returned
        all_hw = client.fetch_course_assignments(course_id=99, uncompleted_only=False)
        assert len(all_hw) == 2


def test_is_assignment_locked():
    from painterv3.canvas_client import is_assignment_locked

    assert not is_assignment_locked(None)
    assert not is_assignment_locked({})

    # Locked flag directly
    assert is_assignment_locked({"locked_for_user": True})
    assert not is_assignment_locked({"locked_for_user": False})

    # Lock at in past (e.g. 2020)
    assert is_assignment_locked({"lock_at": "2020-01-01T00:00:00Z"})

    # Lock at in far future (e.g. 2099)
    assert not is_assignment_locked({"lock_at": "2099-01-01T00:00:00Z"})

    # Unlock at in far future (not unlocked yet)
    assert is_assignment_locked({"unlock_at": "2099-01-01T00:00:00Z"})

    # Lock info present
    assert is_assignment_locked({"lock_info": {"can_view": False}})


def test_fetch_todo_items():
    with patch("painterv3.canvas_client.Canvas") as mock_canvas_cls:
        mock_instance = MagicMock()
        mock_canvas_cls.return_value = mock_instance

        # Normal pending item
        item1 = MagicMock()
        item1.id = "todo_1"
        item1.name = "Normal HW"
        item1.due_at = "2026-10-10T12:00:00Z"
        item1.html_url = "http://todo1"
        item1.course_id = 101
        item1.type = "submitting"
        item1.assignment = {"name": "Normal HW", "locked_for_user": False, "due_at": "2026-10-10T12:00:00Z"}
        item1.quiz = None
        item1.locked_for_user = False
        item1.lock_at = None

        # Locked item
        item2 = MagicMock()
        item2.id = "todo_2"
        item2.assignment = {"name": "Locked HW", "locked_for_user": True}
        item2.quiz = None

        mock_instance.get_todo_items.return_value = [item1, item2]

        client = CanvasClient("https://canvas.test", "tok")
        todos = client.fetch_todo_items()

        assert len(todos) == 1
        assert todos[0]["name"] == "Normal HW"
        assert todos[0]["course_id"] == 101


