# Syaikh & Book Category Grouping Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Group uploaded audio recordings into WordPress Categories (Parent: Syaikh, Child: Book Title) using interactive Telegram Inline Keyboards.

**Architecture:** Extend `bot.py` to interact with WordPress REST API (`/wp-json/wp/v2/categories`) for fetching and creating categories. Use `python-telegram-bot` `CallbackQueryHandler` and `ConversationHandler` to prompt users with inline buttons to select or create Syaikh and Book categories after uploading audio.

**Tech Stack:** Python 3.12, python-telegram-bot v21+, requests, WordPress REST API, Docker / Docker Compose.

## Global Constraints

- No hardcoded credentials — use environment variables.
- Preserve existing ffmpeg and local Telegram Bot API file handling logic.
- Keep dependency count minimal (uses standard `requests` and `python-telegram-bot`).

---

### Task 1: WordPress Category Helpers (`wp_get_categories`, `wp_create_category`)

**Files:**
- Modify: `bot.py`

**Interfaces:**
- Produces:
  - `wp_get_categories() -> list[dict]`
  - `wp_create_category(name: str, parent_id: int | None = None) -> dict`

- [ ] **Step 1: Implement `wp_get_categories` and `wp_create_category` in `bot.py`**

```python
def wp_get_categories() -> list[dict]:
    """Fetch all categories from WP REST API."""
    url = f"{WP_SITE_URL}/wp-json/wp/v2/categories?per_page=100"
    resp = requests.get(
        url,
        auth=(WP_USER, WP_APP_PASSWORD),
        timeout=30,
    )
    resp.raise_for_status()
    return resp.json()

def wp_create_category(name: str, parent_id: int | None = None) -> dict:
    """Create a WP Category (or Subcategory if parent_id provided)."""
    url = f"{WP_SITE_URL}/wp-json/wp/v2/categories"
    payload = {"name": name}
    if parent_id:
        payload["parent"] = parent_id
    resp = requests.post(
        url,
        auth=(WP_USER, WP_APP_PASSWORD),
        json=payload,
        timeout=30,
    )
    resp.raise_for_status()
    return resp.json()
```

- [ ] **Step 2: Update `wp_create_post` signature and body to accept category IDs**

```python
def wp_create_post(title: str, media_id: int, media_url: str, category_ids: list[int] | None = None) -> str:
    """Create WP post with audio block and optional categories."""
    audio_block = (
        f'<!-- wp:audio {{"id":{media_id}}} -->\n'
        f'<figure class="wp-block-audio">'
        f'<audio controls src="{media_url}"></audio>'
        f'</figure>\n'
        f'<!-- /wp:audio -->'
    )

    payload = {
        "title": title,
        "content": audio_block,
        "status": WP_POST_STATUS,
    }
    if category_ids:
        payload["categories"] = category_ids

    resp = requests.post(
        f"{WP_SITE_URL}/wp-json/wp/v2/posts",
        auth=(WP_USER, WP_APP_PASSWORD),
        json=payload,
        timeout=30,
    )
    resp.raise_for_status()
    return resp.json()["link"]
```

- [ ] **Step 3: Commit changes**

```bash
git add bot.py
git commit -m "feat: add WordPress category API helpers and update wp_create_post"
```

---

### Task 2: Interactive Telegram Inline Keyboard & State Flow

**Files:**
- Modify: `bot.py`

**Interfaces:**
- Consumes: `wp_get_categories()`, `wp_create_category()`, `wp_create_post()`
- Produces: `CallbackQueryHandler` logic to let user select existing book, create new book/syaikh, or skip category.

- [ ] **Step 1: Implement inline keyboard menus for Book selection & creation**

In `bot.py`, after audio compression completes, save audio metadata (output path, filename, title) in `context.user_data` and reply with inline action buttons:
1. `book_select`: Lists available child categories (Books) with parent (Syaikh) names.
2. `book_new`: Triggers prompt for new Syaikh & Book creation.
3. `book_skip`: Publishes audio post immediately without categories.

- [ ] **Step 2: Implement callback query handler `handle_category_callback`**

Process user button clicks:
- When user selects a book: publish post with book's category ID and parent Syaikh category ID.
- When user clicks "New Book": prompt user to type/select Syaikh and Book.
- When user clicks "Skip": publish without category.

- [ ] **Step 3: Commit changes**

```bash
git add bot.py
git commit -m "feat: implement inline keyboard category selection in bot.py"
```

---

### Task 3: Deploy & Verify End-to-End

**Files:**
- Modify: `bot.py`

- [ ] **Step 1: Test syntax and imports locally**

Run: `python -m py_compile bot.py`
Expected: Clean compilation with 0 errors.

- [ ] **Step 2: Commit and push changes to GitHub**

```bash
git add bot.py
git commit -m "feat: complete Syaikh and Book category grouping workflow"
git push origin main
```
