"""!
@file canvas_client.py
@brief Canvas LMS REST API integration wrapper.
@details Handles asynchronous thread-pool calls into the canvasapi library,
         HTML cleanup for Discord markdown, submission verification, and To-Do list fetching.
"""

import asyncio
import html
import re
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, Tuple
from canvasapi import Canvas
from canvasapi.exceptions import CanvasException

# ponytail: Synchronous canvasapi executed via asyncio.to_thread.
# Ceiling: High concurrency thread pool contention under tens of thousands of concurrent requests.
# Upgrade path: Direct async HTTP client (aiohttp) calling Canvas REST API directly.

_CANVAS_DATE_FMT = "%Y-%m-%dT%H:%M:%SZ"


def parse_canvas_date(date_str: Optional[str]) -> Optional[datetime]:
    """!
    @brief Parses a Canvas ISO date string into a timezone-aware UTC datetime.
    @param date_str Canvas ISO 8601 formatted string (e.g. '2026-10-10T23:59:59Z').
    @return Timezone-aware UTC datetime object, or None if parsing fails.
    """
    if not date_str:
        return None
    try:
        return datetime.strptime(date_str, _CANVAS_DATE_FMT).replace(tzinfo=timezone.utc)
    except (ValueError, TypeError):
        return None


def clean_html(raw_html: Optional[str], max_length: int = 1000) -> str:
    """!
    @brief Converts Canvas HTML formatted descriptions into clean Discord-friendly markdown.
    @param raw_html The raw HTML string returned by Canvas API.
    @param max_length Maximum character length before truncating with ellipses.
    @return Cleaned markdown string.
    """
    if not raw_html:
        return ""
    text = raw_html

    # Convert links <a href="url">text</a> -> [text](url)
    text = re.sub(r'<a\s+[^>]*href=["\']([^"\']+)["\'][^>]*>(.*?)</a>', r'[\2](\1)', text, flags=re.DOTALL | re.IGNORECASE)

    # Convert bold & italics
    text = re.sub(r'<(?:strong|b)>(.*?)</(?:strong|b)>', r'**\1**', text, flags=re.DOTALL | re.IGNORECASE)
    text = re.sub(r'<(?:em|i)>(.*?)</(?:em|i)>', r'*\1*', text, flags=re.DOTALL | re.IGNORECASE)

    # Convert linebreaks and paragraph endings
    text = re.sub(r'</?(?:p|div|tr|h[1-6])>', '\n', text, flags=re.IGNORECASE)
    text = re.sub(r'<br\s*/?>', '\n', text, flags=re.IGNORECASE)
    text = re.sub(r'<li[^>]*>', '• ', text, flags=re.IGNORECASE)
    text = re.sub(r'</li>', '\n', text, flags=re.IGNORECASE)

    # Strip remaining HTML tags
    text = re.sub(r'<[^>]+>', '', text)
    # Decode HTML entities like &nbsp;, &amp;, etc.
    text = html.unescape(text)

    # Collapse multiple consecutive newlines / whitespace
    text = re.sub(r'\n{3,}', '\n\n', text).strip()

    if len(text) > max_length:
        text = text[:max_length - 3] + "..."
    return text


def is_submission_completed(sub_data: Any) -> bool:
    """!
    @brief Checks whether a Canvas assignment submission indicates completion.
    @details Examines submitted_at timestamp, workflow_state ('submitted', 'graded', 'pending_review'),
             grade, score, and excused status.
    @param sub_data Submission object or dictionary.
    @return True if the assignment submission is completed, False otherwise.
    """
    if not sub_data:
        return False

    if isinstance(sub_data, dict):
        submitted_at = sub_data.get("submitted_at")
        workflow_state = str(sub_data.get("workflow_state", "")).lower()
        excused = sub_data.get("excused", False)
        grade = sub_data.get("grade")
        score = sub_data.get("score")
    else:
        submitted_at = getattr(sub_data, "submitted_at", None)
        workflow_state = str(getattr(sub_data, "workflow_state", "")).lower()
        excused = getattr(sub_data, "excused", False)
        grade = getattr(sub_data, "grade", None)
        score = getattr(sub_data, "score", None)

    if excused is True:
        return True
    if isinstance(submitted_at, str) and submitted_at:
        return True
    if workflow_state in ("submitted", "graded", "pending_review"):
        return True
    if isinstance(score, (int, float)):
        return True
    if isinstance(grade, (int, float, str)) and grade:
        return True

    return False


def is_assignment_locked(assignment: Any) -> bool:
    """!
    @brief Checks whether an assignment is locked or unavailable to the student.
    @param assignment Canvas assignment object or dictionary.
    @return True if locked or unavailable, False otherwise.
    """
    if not assignment:
        return False

    if isinstance(assignment, dict):
        locked_val = assignment.get("locked_for_user", False)
        lock_at = assignment.get("lock_at")
        unlock_at = assignment.get("unlock_at")
        lock_info = assignment.get("lock_info")
    else:
        locked_val = getattr(assignment, "locked_for_user", False)
        lock_at = getattr(assignment, "lock_at", None)
        unlock_at = getattr(assignment, "unlock_at", None)
        lock_info = getattr(assignment, "lock_info", None)

    if locked_val is True:
        return True

    if isinstance(lock_info, dict) and lock_info:
        return True

    now = datetime.now(timezone.utc)

    if isinstance(lock_at, str):
        lock_dt = parse_canvas_date(lock_at)
        if lock_dt and now >= lock_dt:
            return True

    if isinstance(unlock_at, str):
        unlock_dt = parse_canvas_date(unlock_at)
        if unlock_dt and now < unlock_dt:
            return True

    return False


class CanvasClient:
    """!
    @brief Wrapper client for interacting with a specific Canvas LMS institution.
    """

    def __init__(self, base_url: str, token: str):
        """!
        @brief Initializes Canvas API client.
        @param base_url Base URL of Canvas institution (e.g. 'https://tip.instructure.com').
        @param token User API access token.
        """
        self.base_url = base_url.rstrip("/")
        self.token = token
        self._canvas = Canvas(self.base_url, self.token)

    def validate_and_get_profile(self) -> Tuple[Dict[str, Any], List[Dict[str, Any]]]:
        """!
        @brief Validates the token and retrieves user profile with active section enrollments.
        @return Tuple of (user_info: dict, enrollments: list[dict]).
        """
        user = self._canvas.get_current_user()
        user_info = {
            "id": user.id,
            "name": getattr(user, "name", "Unknown"),
            "email": getattr(user, "email", None),
        }

        # Include sections in enrollment query
        courses = list(user.get_courses(enrollment_state="active", include=["sections"]))
        enrollments = []
        for course in courses:
            c_name = getattr(course, "name", f"Course {course.id}")
            sections = getattr(course, "sections", [])
            if sections:
                for s in sections:
                    enrollments.append({
                        "course_id": course.id,
                        "section_id": s["id"],
                        "course_name": c_name,
                        "section_name": s.get("name", f"Section {s['id']}"),
                    })
            else:
                enrollments.append({
                    "course_id": course.id,
                    "section_id": 0,
                    "course_name": c_name,
                    "section_name": "General",
                })

        return user_info, enrollments

    def fetch_announcements_batch(self, course_ids: List[int]) -> List[Dict[str, Any]]:
        """!
        @brief Fetches announcements in bulk using Canvas context codes.
        @param course_ids List of Canvas course IDs.
        @return List of parsed announcement dictionaries.
        """
        if not course_ids:
            return []

        context_codes = [f"course_{cid}" for cid in course_ids]
        try:
            announcements = list(self._canvas.get_announcements(context_codes=context_codes))
        except CanvasException as e:
            print(f"[CanvasClient] Error fetching announcements: {e}")
            return []

        results = []
        for a in announcements:
            ctx = getattr(a, "context_code", "")
            course_id = int(ctx.split("_")[1]) if "_" in ctx else 0
            results.append({
                "id": str(a.id),
                "course_id": course_id,
                "title": getattr(a, "title", "No Title"),
                "author": getattr(a, "user_name", "Instructor"),
                "posted_at": getattr(a, "posted_at", None),
                "message_raw": getattr(a, "message", ""),
                "message_clean": clean_html(getattr(a, "message", "")),
                "url": getattr(a, "html_url", getattr(a, "url", "")),
                "is_section_specific": getattr(a, "is_section_specific", False),
            })
        return results

    def fetch_course_assignments(
        self, course_id: int, uncompleted_only: bool = False
    ) -> List[Dict[str, Any]]:
        """!
        @brief Fetches assignments for a specific course.
        @param course_id Canvas Course ID.
        @param uncompleted_only Filter out completed or locked assignments if True.
        @return List of assignment dictionaries.
        """
        try:
            course = self._canvas.get_course(course_id)
            assignments = list(course.get_assignments(include=["submission"]))
        except CanvasException as e:
            print(f"[CanvasClient] Error fetching assignments for course {course_id}: {e}")
            return []

        results = []
        for a in assignments:
            sub = getattr(a, "submission", None)
            completed = is_submission_completed(sub)
            locked = is_assignment_locked(a)
            if uncompleted_only and (completed or locked):
                continue

            results.append({
                "id": str(a.id),
                "course_id": course_id,
                "name": getattr(a, "name", "Untitled Assignment"),
                "due_at": getattr(a, "due_at", None),
                "points_possible": getattr(a, "points_possible", None),
                "url": getattr(a, "html_url", ""),
                "description_clean": clean_html(getattr(a, "description", "")),
                "is_completed": completed,
                "is_locked": locked,
                "submission": sub,
            })
        return results

    def fetch_todo_items(self) -> List[Dict[str, Any]]:
        """!
        @brief Fetches the current user's To-Do list from Canvas (/api/v1/users/self/todo).
        @return List of pending To-Do items.
        """
        try:
            todos = list(self._canvas.get_todo_items())
        except CanvasException as e:
            print(f"[CanvasClient] Error fetching todo items: {e}")
            return []

        results = []
        for item in todos:
            assignment = getattr(item, "assignment", None)
            quiz = getattr(item, "quiz", None)

            # Skip locked items
            if is_assignment_locked(assignment) or is_assignment_locked(quiz) or is_assignment_locked(item):
                continue

            name = None
            due_at = None
            url = getattr(item, "html_url", "")
            points = None
            course_id = getattr(item, "course_id", None)

            if isinstance(assignment, dict):
                name = assignment.get("name")
                due_at = assignment.get("due_at")
                url = assignment.get("html_url") or url
                points = assignment.get("points_possible")
                course_id = assignment.get("course_id") or course_id
            elif assignment:
                name = getattr(assignment, "name", None)
                due_at = getattr(assignment, "due_at", None)
                url = getattr(assignment, "html_url", url)
                points = getattr(assignment, "points_possible", None)
                course_id = getattr(assignment, "course_id", course_id)
            elif isinstance(quiz, dict):
                name = quiz.get("title")
                due_at = quiz.get("due_at")
                url = quiz.get("html_url") or url
                points = quiz.get("points_possible")
            elif quiz:
                name = getattr(quiz, "title", None)
                due_at = getattr(quiz, "due_at", None)
                url = getattr(quiz, "html_url", url)
                points = getattr(quiz, "points_possible", None)

            if not name:
                name = getattr(item, "name", getattr(item, "title", "To-Do Item"))
            if not due_at:
                due_at = getattr(item, "due_at", None)

            results.append({
                "id": str(getattr(item, "id", name)),
                "name": name,
                "due_at": due_at,
                "points_possible": points,
                "url": url,
                "course_id": course_id,
                "type": getattr(item, "type", "submitting"),
            })

        return results

    def check_user_assignment_completion(self, course_id: int, assignment_id: int) -> bool:
        """!
        @brief Checks if the authenticated user has completed a specific assignment or if locked.
        @param course_id Canvas Course ID.
        @param assignment_id Canvas Assignment ID.
        @return True if completed or locked, False otherwise.
        """
        try:
            course = self._canvas.get_course(course_id)
            assignment = course.get_assignment(assignment_id, include=["submission"])
            if is_assignment_locked(assignment):
                return True
            sub = getattr(assignment, "submission", None)
            return is_submission_completed(sub)
        except CanvasException as e:
            print(f"[CanvasClient] Error checking assignment completion ({assignment_id}): {e}")
            return False

    def get_user_submission(self, course_id: int, assignment_id: int) -> Optional[Dict[str, Any]]:
        """!
        @brief Fetches verified submission details directly from Canvas API using user credentials.
        @note Prevents false self-reports by inspecting Canvas submission timestamps.
        @param course_id Canvas Course ID.
        @param assignment_id Canvas Assignment ID.
        @return Dictionary containing submission details or None if not submitted.
        """
        try:
            course = self._canvas.get_course(course_id)
            assignment = course.get_assignment(assignment_id, include=["submission"])
            sub = getattr(assignment, "submission", None)
            if not sub:
                return None
            if isinstance(sub, dict):
                submitted_at = sub.get("submitted_at")
                workflow_state = sub.get("workflow_state")
            else:
                submitted_at = getattr(sub, "submitted_at", None)
                workflow_state = getattr(sub, "workflow_state", None)

            completed = is_submission_completed(sub)
            return {
                "assignment_id": str(assignment_id),
                "course_id": course_id,
                "submitted_at": submitted_at,
                "workflow_state": workflow_state,
                "is_completed": completed,
            }
        except CanvasException as e:
            print(f"[CanvasClient] Error fetching verified submission ({assignment_id}): {e}")
            return None


# ponytail: Simple dict cache for CanvasClient instances.
# Ceiling: Unbounded growth if tokens are frequently rotated.
# Upgrade path: LRU with functools.lru_cache or bounded dict with eviction.
_client_cache: Dict[Tuple[str, str], "CanvasClient"] = {}


def _get_client(url: str, token: str) -> "CanvasClient":
    """!
    @brief Returns or creates a cached CanvasClient instance.
    @param url Canvas institution base URL.
    @param token Canvas user access token.
    @return CanvasClient instance.
    """
    key = (url, token)
    client = _client_cache.get(key)
    if client is None:
        client = CanvasClient(url, token)
        _client_cache[key] = client
    return client


async def async_validate_and_get_profile(url: str, token: str) -> Tuple[Dict[str, Any], List[Dict[str, Any]]]:
    """!
    @brief Async wrapper to validate token and fetch profile with section enrollments.
    """
    client = _get_client(url, token)
    return await asyncio.to_thread(client.validate_and_get_profile)


async def async_fetch_announcements(url: str, token: str, course_ids: List[int]) -> List[Dict[str, Any]]:
    """!
    @brief Async wrapper to fetch announcements for courses in bulk.
    """
    client = _get_client(url, token)
    return await asyncio.to_thread(client.fetch_announcements_batch, course_ids)


async def async_fetch_assignments(
    url: str, token: str, course_id: int, uncompleted_only: bool = False
) -> List[Dict[str, Any]]:
    """!
    @brief Async wrapper to fetch course assignments.
    """
    client = _get_client(url, token)
    return await asyncio.to_thread(client.fetch_course_assignments, course_id, uncompleted_only)


async def async_fetch_todo_items(url: str, token: str) -> List[Dict[str, Any]]:
    """!
    @brief Async wrapper to fetch user's Canvas To-Do list items.
    """
    client = _get_client(url, token)
    return await asyncio.to_thread(client.fetch_todo_items)


async def async_check_user_assignment_completion(
    url: str, token: str, course_id: int, assignment_id: int
) -> bool:
    """!
    @brief Async wrapper to verify user assignment completion status.
    """
    client = _get_client(url, token)
    return await asyncio.to_thread(client.check_user_assignment_completion, course_id, assignment_id)


async def async_get_user_submission(
    url: str, token: str, course_id: int, assignment_id: int
) -> Optional[Dict[str, Any]]:
    """!
    @brief Async wrapper to retrieve verified submission details from Canvas API.
    """
    client = _get_client(url, token)
    return await asyncio.to_thread(client.get_user_submission, course_id, assignment_id)
