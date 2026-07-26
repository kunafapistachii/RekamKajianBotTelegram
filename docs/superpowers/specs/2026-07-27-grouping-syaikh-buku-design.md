# Design Specification: Syaikh & Book Grouping via WordPress Categories

Date: 2026-07-27
Status: Approved

## Overview
Enhance the Telegram Auto Upload Kajian bot to organize uploaded lecture audios into WordPress Categories and Subcategories representing Syaikh (Teacher) and Buku (Book Title).

## Architecture & Workflow

### 1. WordPress Taxonomy Structure
- **Parent Category (Syaikh)**: e.g. `Syaikh Muhammad bin Shalih Al-Utsaimin`
- **Child Category (Buku)**: e.g. `Syarah Kitabut Tauhid` (Parent set to Syaikh Category ID)
- **Post Assignment**: Each published post is assigned both the Book Category ID and its Parent Syaikh Category ID.

### 2. Telegram Bot Interaction (Inline Keyboard & ConversationState)
1. **Audio Upload**: User sends audio/voice file to the Telegram bot.
2. **Processing**: Bot downloads and compresses the audio using ffmpeg.
3. **Selection Menu**: Bot sends an inline keyboard with 3 options:
   - 📚 **[Pilih Buku Existing]**: Fetches categories from WordPress REST API (`GET /wp-json/wp/v2/categories`). Displays available Books with their parent Syaikh name.
   - ➕ **[+ Tambah Buku Baru]**: Prompts user to select/enter Syaikh name and Book title. Bot creates parent (Syaikh) and child (Book) categories via WP REST API (`POST /wp-json/wp/v2/categories`).
   - ⏩ **[Tanpa Buku / General]**: Uploads post under default/uncategorized category.
4. **Publication**: Bot uploads compressed audio to WordPress Media Library, creates the post with assigned categories, and replies with the published post URL.

## WordPress API Integration
- `GET /wp-json/wp/v2/categories?per_page=100`: Fetches existing categories and builds parent-child mapping.
- `POST /wp-json/wp/v2/categories`: Creates new Syaikh (parent) or Book (child) categories using HTTP Basic Auth.
- `POST /wp-json/wp/v2/posts`: Creates the post with `"categories": [book_cat_id, syaikh_cat_id]`.

## Error Handling & Cleanups
- If category creation fails, fallback gracefully and notify user in Telegram.
- Temporary files are deleted after completion or failure.
