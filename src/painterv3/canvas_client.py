import asyncio
import html
import re
from datetime import datetime, timezone
from typing import Optional, List, Dict, Any, Tuple
from canvasapi import Canvas
from canvasapi.exceptions import CanvasException

# ponytail: Synchronous canvasapi executed via asyncio.to_thread.
# Ceiling: High concurrency thread pool contention under tens of thousands of concurrent requests.
# Upgrade path: Direct async HTTP client (aiohttp) calling Canvas REST API directly.

_CANVAS_DATE_FMT = "%Y-%m-%dT%H:%M:%SZ"


def parse_canvas_date(date_str: Optional[str]) -> Optional[datetime]:
    """Parse a Canvas ISO date string into a timezone-aware datetime, or None on failure."""
    if not date_str:
        return None
    try:
        return datetime.strptime(date_str, _CANVAS_DATE_FMT).replace(tzinfo=timezone.utc)
    except (ValueError, TypeError):
        return None


def clean_html(raw_html: Optional[str], max_length: int = 1000) -> str:
    """Converts Canvas HTML formatted text to clean, readable Discord-friendly markdown text."""
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
    """Checks whether a Canvas assignment submission indicates completion."""
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
    """Checks whether an assignment is locked or unavailable to the student."""
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
    def __init__(self, base_url: str, token: str):
        self.base_url = base_url.rstrip("/")
        self.token = token
        self._canvas = Canvas(self.base_url, self.token)

    def validate_and_get_profile(self) -> Tuple[Dict[str, Any], List[Dict[str, Any]]]:
        """
        Validates the token, retrieves current user info, and returns user info + active enrollments.
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
        """
        Fetches announcements in bulk using Canvas context codes.
        Optimized to batch up to 50 courses per request.
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
        """Fetches active assignments for a given course, optionally filtering for uncompleted only."""
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
            })
        return results

    def fetch_todo_items(self) -> List[Dict[str, Any]]:
        """
        Fetches the current user's To-Do list from Canvas (/api/v1/users/self/todo),
        filtering out completed or locked assignments.
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
        """Checks if the user has completed a specific assignment or if it is locked."""
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


# --- Async Wrappers ---

async def async_validate_and_get_profile(url: str, token: str) -> Tuple[Dict[str, Any], List[Dict[str, Any]]]:
    client = CanvasClient(url, token)
    return await asyncio.to_thread(client.validate_and_get_profile)


async def async_fetch_announcements(url: str, token: str, course_ids: List[int]) -> List[Dict[str, Any]]:
    client = CanvasClient(url, token)
    return await asyncio.to_thread(client.fetch_announcements_batch, course_ids)


async def async_fetch_assignments(
    url: str, token: str, course_id: int, uncompleted_only: bool = False
) -> List[Dict[str, Any]]:
    client = CanvasClient(url, token)
    return await asyncio.to_thread(client.fetch_course_assignments, course_id, uncompleted_only)


async def async_fetch_todo_items(url: str, token: str) -> List[Dict[str, Any]]:
    client = CanvasClient(url, token)
    return await asyncio.to_thread(client.fetch_todo_items)


async def async_check_user_assignment_completion(
    url: str, token: str, course_id: int, assignment_id: int
) -> bool:
    client = CanvasClient(url, token)
    return await asyncio.to_thread(client.check_user_assignment_completion, course_id, assignment_id)
