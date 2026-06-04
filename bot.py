import os
import sqlite3
from telegram import Update
from telegram.ext import (
    Application,
    CommandHandler,
    MessageHandler,
    ContextTypes,
    filters,
)

TOKEN = os.environ["BOT_TOKEN"]  # Set this in Railway environment variables

# ==========================
# DATABASE
# ==========================

# On Railway: DB_PATH=/data/inventory.db (persistent volume)
# Locally: falls back to inventory.db in current folder
DB_PATH = os.environ.get("DB_PATH", "inventory.db")
os.makedirs(os.path.dirname(DB_PATH) if os.path.dirname(DB_PATH) else ".", exist_ok=True)

conn = sqlite3.connect(DB_PATH, check_same_thread=False)
cur = conn.cursor()

cur.execute("""
CREATE TABLE IF NOT EXISTS inventory (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    barcode TEXT NOT NULL,
    row_name TEXT NOT NULL,
    position INTEGER NOT NULL,
    UNIQUE(barcode, row_name, position)
)
""")

# Separate table: one photo per barcode (optional)
cur.execute("""
CREATE TABLE IF NOT EXISTS photos (
    barcode TEXT PRIMARY KEY,
    file_id TEXT NOT NULL
)
""")

conn.commit()

# ==========================
# HELPERS
# ==========================

def get_next_position(row_name):
    cur.execute(
        "SELECT COALESCE(MAX(position), 0) FROM inventory WHERE row_name=?",
        (row_name,)
    )
    return cur.fetchone()[0] + 1


def get_photo_file_id(barcode):
    cur.execute("SELECT file_id FROM photos WHERE barcode=?", (barcode,))
    row = cur.fetchone()
    return row[0] if row else None


HELP_TEXT = """
👟 *Puma Depot Inventory Bot*

*📦 Adding items:*
/add `BARCODE ROW POSITION` — Add a single item
/addrow `ROW item1 item2 ...` — Create a row with items
/appendrow `ROW item1 item2 ...` — Append items to an existing row

*📷 Photos (optional):*
/addphoto `BARCODE` — Link a photo to a barcode (then send the photo)
/removephoto `BARCODE` — Remove photo from a barcode

*🔍 Searching:*
/find `BARCODE` — Find where a barcode is stored (shows photo if set)
/showrow `ROW` — Show all items in a row
/rows — List all existing rows

*✏️ Editing:*
/rename `OLD_ROW NEW_ROW` — Rename a row
/delete `BARCODE ROW POSITION` — Remove a specific barcode entry
/deleterow `ROW` — Delete an entire row and all its items

*⚠️ Admin:*
/clear — Wipe the entire database (items + photos)

Use /help anytime to see this list.
"""

# ==========================
# /START
# ==========================

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        f"👋 Welcome to the Puma Depot Inventory Bot!\n{HELP_TEXT}",
        parse_mode="Markdown"
    )

# ==========================
# /HELP
# ==========================

async def help_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(HELP_TEXT, parse_mode="Markdown")

# ==========================
# /ADD
# ==========================

async def add(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if len(context.args) != 3:
        await update.message.reply_text(
            "❌ Usage:\n`/add BARCODE ROW POSITION`",
            parse_mode="Markdown"
        )
        return

    barcode = context.args[0]
    row_name = context.args[1]

    try:
        position = int(context.args[2])
    except ValueError:
        await update.message.reply_text("❌ Position must be a number.")
        return

    try:
        cur.execute(
            "INSERT INTO inventory (barcode, row_name, position) VALUES (?, ?, ?)",
            (barcode, row_name, position)
        )
        conn.commit()
        await update.message.reply_text(
            f"✅ Saved `{barcode}` → *{row_name}*, position {position}\n\n"
            f"💡 Tip: use `/addphoto {barcode}` to attach a photo.",
            parse_mode="Markdown"
        )
    except sqlite3.IntegrityError:
        await update.message.reply_text(
            f"⚠️ `{barcode}` already exists at *{row_name}*, position {position}.",
            parse_mode="Markdown"
        )

# ==========================
# /ADDROW
# ==========================

async def addrow(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if len(context.args) < 2:
        await update.message.reply_text(
            "❌ Usage:\n`/addrow ROW_NAME item1 item2 ...`\n"
            "Comma-separate barcodes at the same position:\n"
            "`/addrow A1 barcode1,barcode2 barcode3`",
            parse_mode="Markdown"
        )
        return

    row_name = context.args[0]
    groups = context.args[1:]
    count = 0
    skipped = 0

    for position, group in enumerate(groups, start=1):
        barcodes = [x.strip() for x in group.split(",") if x.strip()]
        for barcode in barcodes:
            try:
                cur.execute(
                    "INSERT INTO inventory (barcode, row_name, position) VALUES (?, ?, ?)",
                    (barcode, row_name, position)
                )
                count += 1
            except sqlite3.IntegrityError:
                skipped += 1

    conn.commit()

    msg = f"✅ Added *{count}* barcode(s) to row *{row_name}*."
    if skipped:
        msg += f"\n⚠️ Skipped {skipped} duplicate(s)."
    await update.message.reply_text(msg, parse_mode="Markdown")

# ==========================
# /APPENDROW
# ==========================

async def appendrow(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if len(context.args) < 2:
        await update.message.reply_text(
            "❌ Usage:\n`/appendrow ROW_NAME item1 item2 ...`",
            parse_mode="Markdown"
        )
        return

    row_name = context.args[0]
    groups = context.args[1:]
    position = get_next_position(row_name)
    count = 0
    skipped = 0

    for group in groups:
        barcodes = [x.strip() for x in group.split(",") if x.strip()]
        for barcode in barcodes:
            try:
                cur.execute(
                    "INSERT INTO inventory (barcode, row_name, position) VALUES (?, ?, ?)",
                    (barcode, row_name, position)
                )
                count += 1
            except sqlite3.IntegrityError:
                skipped += 1
        position += 1

    conn.commit()

    msg = f"✅ Appended *{count}* barcode(s) to row *{row_name}*."
    if skipped:
        msg += f"\n⚠️ Skipped {skipped} duplicate(s)."
    await update.message.reply_text(msg, parse_mode="Markdown")

# ==========================
# /ADDPHOTO  — step 1: register intent
# ==========================

async def addphoto(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if len(context.args) != 1:
        await update.message.reply_text(
            "❌ Usage:\n`/addphoto BARCODE`\nThen immediately send the photo.",
            parse_mode="Markdown"
        )
        return

    barcode = context.args[0]

    cur.execute("SELECT COUNT(*) FROM inventory WHERE barcode=?", (barcode,))
    if cur.fetchone()[0] == 0:
        await update.message.reply_text(
            f"❌ Barcode `{barcode}` not found in inventory.\n"
            f"Add it first with /add.",
            parse_mode="Markdown"
        )
        return

    # Save intent — the photo message handler will pick this up
    context.user_data["pending_photo_barcode"] = barcode

    existing = get_photo_file_id(barcode)
    note = " _(replaces existing photo)_" if existing else ""
    await update.message.reply_text(
        f"📷 Ready! Now send the photo for `{barcode}`{note}.",
        parse_mode="Markdown"
    )

# ==========================
# PHOTO RECEIVER — step 2: handle the photo
# ==========================

async def receive_photo(update: Update, context: ContextTypes.DEFAULT_TYPE):
    barcode = context.user_data.get("pending_photo_barcode")

    if not barcode:
        # User sent a photo with no pending /addphoto — ignore
        return

    # Telegram provides several sizes; pick the largest (best quality)
    file_id = update.message.photo[-1].file_id

    cur.execute(
        """
        INSERT INTO photos (barcode, file_id) VALUES (?, ?)
        ON CONFLICT(barcode) DO UPDATE SET file_id=excluded.file_id
        """,
        (barcode, file_id)
    )
    conn.commit()

    context.user_data.pop("pending_photo_barcode", None)

    await update.message.reply_text(
        f"✅ Photo saved for `{barcode}`!\n"
        f"Use `/find {barcode}` to see it.",
        parse_mode="Markdown"
    )

# ==========================
# /REMOVEPHOTO
# ==========================

async def removephoto(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if len(context.args) != 1:
        await update.message.reply_text(
            "❌ Usage:\n`/removephoto BARCODE`",
            parse_mode="Markdown"
        )
        return

    barcode = context.args[0]
    cur.execute("DELETE FROM photos WHERE barcode=?", (barcode,))
    conn.commit()

    if cur.rowcount == 0:
        await update.message.reply_text(
            f"⚠️ No photo found for `{barcode}`.",
            parse_mode="Markdown"
        )
    else:
        await update.message.reply_text(
            f"🗑️ Photo removed for `{barcode}`.",
            parse_mode="Markdown"
        )

# ==========================
# /FIND
# ==========================

async def find(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if len(context.args) != 1:
        await update.message.reply_text(
            "❌ Usage:\n`/find BARCODE`",
            parse_mode="Markdown"
        )
        return

    barcode = context.args[0]

    cur.execute(
        "SELECT row_name, position FROM inventory WHERE barcode=? ORDER BY row_name, position",
        (barcode,)
    )
    results = cur.fetchall()

    if not results:
        await update.message.reply_text(
            f"❌ Barcode `{barcode}` not found.",
            parse_mode="Markdown"
        )
        return

    text = f"🔍 *{barcode}*\n\n"
    for row_name, position in results:
        text += f"• *{row_name}* → position {position}\n"

    file_id = get_photo_file_id(barcode)

    if file_id:
        # Show photo with location info as caption
        await update.message.reply_photo(
            photo=file_id,
            caption=text,
            parse_mode="Markdown"
        )
    else:
        text += "\n_No photo attached. Use /addphoto to add one._"
        await update.message.reply_text(text, parse_mode="Markdown")

# ==========================
# /SHOWROW
# ==========================

async def showrow(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if len(context.args) != 1:
        await update.message.reply_text(
            "❌ Usage:\n`/showrow ROW_NAME`",
            parse_mode="Markdown"
        )
        return

    row_name = context.args[0]

    cur.execute(
        "SELECT position, barcode FROM inventory WHERE row_name=? ORDER BY position",
        (row_name,)
    )
    results = cur.fetchall()

    if not results:
        await update.message.reply_text(
            f"❌ Row *{row_name}* not found.",
            parse_mode="Markdown"
        )
        return

    positions = {}
    for position, barcode in results:
        positions.setdefault(position, []).append(barcode)

    text = f"📦 Row *{row_name}*:\n\n"
    for position in sorted(positions):
        barcodes_here = positions[position]
        # Mark barcodes that have a photo with 📷
        labeled = []
        for b in barcodes_here:
            labeled.append(f"{b} 📷" if get_photo_file_id(b) else b)
        text += f"`{position}.` {', '.join(labeled)}\n"

    text += "\n_📷 = has photo — use /find BARCODE to view_"
    await update.message.reply_text(text, parse_mode="Markdown")

# ==========================
# /ROWS
# ==========================

async def rows(update: Update, context: ContextTypes.DEFAULT_TYPE):
    cur.execute(
        "SELECT row_name, COUNT(*) FROM inventory GROUP BY row_name ORDER BY row_name"
    )
    results = cur.fetchall()

    if not results:
        await update.message.reply_text("📭 No rows in the database yet.")
        return

    text = "📋 *All rows:*\n\n"
    for row_name, total in results:
        text += f"• *{row_name}* — {total} barcode(s)\n"

    await update.message.reply_text(text, parse_mode="Markdown")

# ==========================
# /RENAME
# ==========================

async def rename(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if len(context.args) != 2:
        await update.message.reply_text(
            "❌ Usage:\n`/rename OLD_ROW NEW_ROW`",
            parse_mode="Markdown"
        )
        return

    old_name, new_name = context.args[0], context.args[1]

    cur.execute("SELECT COUNT(*) FROM inventory WHERE row_name=?", (old_name,))
    count = cur.fetchone()[0]

    if count == 0:
        await update.message.reply_text(
            f"❌ Row *{old_name}* not found.",
            parse_mode="Markdown"
        )
        return

    cur.execute("UPDATE inventory SET row_name=? WHERE row_name=?", (new_name, old_name))
    conn.commit()

    await update.message.reply_text(
        f"✅ Renamed *{old_name}* → *{new_name}* ({count} item(s) updated).",
        parse_mode="Markdown"
    )

# ==========================
# /DELETE
# ==========================

async def delete(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if len(context.args) != 3:
        await update.message.reply_text(
            "❌ Usage:\n`/delete BARCODE ROW POSITION`",
            parse_mode="Markdown"
        )
        return

    barcode, row_name = context.args[0], context.args[1]

    try:
        position = int(context.args[2])
    except ValueError:
        await update.message.reply_text("❌ Position must be a number.")
        return

    cur.execute(
        "DELETE FROM inventory WHERE barcode=? AND row_name=? AND position=?",
        (barcode, row_name, position)
    )
    conn.commit()

    if cur.rowcount == 0:
        await update.message.reply_text(
            f"❌ No entry found for `{barcode}` in *{row_name}* at position {position}.",
            parse_mode="Markdown"
        )
    else:
        await update.message.reply_text(
            f"🗑️ Deleted `{barcode}` from *{row_name}*, position {position}.",
            parse_mode="Markdown"
        )

# ==========================
# /DELETEROW
# ==========================

async def deleterow(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if len(context.args) != 1:
        await update.message.reply_text(
            "❌ Usage:\n`/deleterow ROW_NAME`",
            parse_mode="Markdown"
        )
        return

    row_name = context.args[0]

    cur.execute("SELECT COUNT(*) FROM inventory WHERE row_name=?", (row_name,))
    count = cur.fetchone()[0]

    if count == 0:
        await update.message.reply_text(
            f"❌ Row *{row_name}* not found.",
            parse_mode="Markdown"
        )
        return

    cur.execute("DELETE FROM inventory WHERE row_name=?", (row_name,))
    conn.commit()

    await update.message.reply_text(
        f"🗑️ Deleted row *{row_name}* and its {count} barcode(s).",
        parse_mode="Markdown"
    )

# ==========================
# /CLEAR
# ==========================

async def clear(update: Update, context: ContextTypes.DEFAULT_TYPE):
    cur.execute("DELETE FROM inventory")
    cur.execute("DELETE FROM photos")
    conn.commit()
    await update.message.reply_text("⚠️ Database cleared (all items and photos removed).")

# ==========================
# BOT SETUP
# ==========================

app = Application.builder().token(TOKEN).build()

app.add_handler(CommandHandler("start",       start))
app.add_handler(CommandHandler("help",        help_command))
app.add_handler(CommandHandler("add",         add))
app.add_handler(CommandHandler("addrow",      addrow))
app.add_handler(CommandHandler("appendrow",   appendrow))
app.add_handler(CommandHandler("addphoto",    addphoto))
app.add_handler(CommandHandler("removephoto", removephoto))
app.add_handler(CommandHandler("find",        find))
app.add_handler(CommandHandler("showrow",     showrow))
app.add_handler(CommandHandler("rows",        rows))
app.add_handler(CommandHandler("rename",      rename))
app.add_handler(CommandHandler("delete",      delete))
app.add_handler(CommandHandler("deleterow",   deleterow))
app.add_handler(CommandHandler("clear",       clear))

# Photo handler must come AFTER all command handlers
app.add_handler(MessageHandler(filters.PHOTO, receive_photo))

print("Bot running...")
app.run_polling()
