# PainterV3 - Discord Bot with Canvas API Integration

A high-performance Discord Bot integrating Canvas LMS with Discord using `discord.py` and `canvasapi`.

## Features

- **`/login` Slash Command**:
  - Interactive Discord Modal prompting for Canvas API access token and instance URL (defaults to `https://tip.instructure.com`).
  - Real-time token validation and automated discovery of active courses and section enrollments.
  - Secure local storage inside SQLite.
- **Automated Direct Message (DM) Announcements**:
  - Automatically sends formatted DMs with embeds to all registered students enrolled in a course whenever new announcements are posted.
  - Deduplication prevents duplicate DMs even across restarts.
  - Automatically handles users with closed DMs without crashing or spamming.
- **12-Hour Deadline Reminder DMs**:
  - Caches assignments and monitors approaching deadlines.
  - Within 12 hours of an assignment deadline, queries the student's submission status.
  - If the student has not completed the assignment, automatically sends a high-priority reminder DM with live countdowns (`<t:timestamp:R>`).
  - Automatically skips students who have already turned in the assignment or if the task is locked.
  - Deduped in-memory and in SQLite so students are never spammed twice for the same task.
- **Server Section Binding**:
  - **/server_setup**: Admin command to bind a Canvas section ID to the Discord server, designating an announcements channel and an assignments channel.
  - **/server_status**: View current section bindings and eligible donor counts.
  - **/server_unlink**: Remove server configuration.
- **Section Donor Account Rotation ("Changes Every Time")**:
  - When fetching section-wide announcements and assignments for a Discord server, the bot selects from registered students in that section in a round-robin rotation.
  - Spreads API rate limit usage across accounts and automatically selects the next donor if an account fails.
- **Student Utility Commands (Single canonical command per feature)**:
  - **/my_courses**: View connected Canvas profile and list of registered courses and sections.
  - **/my_assignments**: View assignments with customizable filters:
    - `course`: Filter by specific course (with autocomplete dropdown).
    - `include_undated`: Include assignments without a due date (default: `False`).
    - `past_due`: Show assignments past their deadline (default: `False`, shows upcoming).
    - `completed_only`: Show only completed assignments (default: `False`, shows pending).
    - Automatically filters out locked tasks and formats due dates with `<t:timestamp:F>`.
  - **/todo**: Pulls your active Canvas To-Do list items (`/api/v1/users/self/todo`), excluding locked tasks.
  - **/logout**: Unlink Canvas account and wipe stored token.
  - **/sync_now**: Force an immediate synchronization pass.
  - **`!sync`**: Prefix command for bot owners to instantly push slash commands to the current server.

---

### Canvas OAuth2 vs. Manual Token: Tradeoffs & Security

- **Canvas OAuth2 Flow**:
  - Requires creating a Developer Key in Canvas under `Admin -> Developer Keys`.
  - **The Catch**: Only institutional Canvas Site Administrators (e.g. university IT departments at `tip.instructure.com`) have permissions to generate Developer Keys. Regular students and instructors cannot create OAuth2 Developer Keys.
  - In addition, OAuth2 requires a public HTTPS redirect URI, necessitating a web server callback endpoint.
- **Manual Personal Access Token (The Standard Approach)**:
  - Any student can generate a personal access token under Canvas `Account -> Settings -> Approved Integrations -> + New Access Token`.
  - Collected via Discord's secure, ephemeral Modal dialog (`/login`) so tokens never appear in server chat logs.
  - Stored locally in SQLite and can be deleted at any time with `/logout` or by revoking the token directly in Canvas settings.

## Architecture & Optimizations

- **Zero-Unnecessary-Dependencies (Ponytail Principles)**:
  - Standard library `sqlite3` with WAL mode (`PRAGMA journal_mode=WAL`), `NORMAL` synchronous writes, and foreign key cascades.
- **In-Memory Delivery Caching**:
  - Pre-loads all past delivery hashes into Python memory sets at startup for $O(1)$ deduplication checks during background loops, avoiding repetitive disk queries.
- **Non-blocking Canvas API Calls**:
  - `canvasapi` runs on worker threads via `asyncio.to_thread` so the Discord gateway event loop is never blocked.
- **Bulk Batching**:
  - Canvas announcements are queried using combined `context_codes` (`course_X`, `course_Y`) in a single API call instead of $N$ individual HTTP requests.
- **Clean Markdown Formatter**:
  - Converts raw Canvas HTML into clean Discord-flavored Markdown (parsing links, bolding, italics, bullets, linebreaks, and stripping leftover tags).

---

## Configuration (`.env`)

```env
BOT_TOKEN=your_discord_bot_token
CANVAS_API_URL=https://tip.instructure.com
```

---

## Installation & Running

Using `uv`:

```bash
# Sync dependencies
uv sync

# Run the bot
uv run python main.py
```

---

## Deployment with Docker

### Option 1: Docker Compose (Recommended)

1. Ensure your `.env` is configured:
   ```bash
   cp .env.example .env
   # Set BOT_TOKEN and optional CANVAS_API_URL
   ```

2. Build and start the bot container:
   ```bash
   docker compose up -d --build
   ```

3. View live logs:
   ```bash
   docker compose logs -f
   ```

4. Stop the container:
   ```bash
   docker compose down
   ```

### Option 2: Docker CLI

1. Build the image:
   ```bash
   docker build -t painterv3:latest .
   ```

2. Run with persistent volume for SQLite database storage:
   ```bash
   docker run -d \
     --name painterv3-bot \
     --restart unless-stopped \
     --env-file .env \
     -v painterv3-data:/app/data \
     painterv3:latest
   ```

3. View logs:
   ```bash
   docker logs -f painterv3-bot
   ```

---

## Running Tests

All database operations, donor rotation algorithms, Canvas data parsing, slash commands, and synchronization flows are covered by automated tests:

```bash
uv run pytest
```
