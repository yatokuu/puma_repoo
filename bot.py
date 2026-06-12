import os
import sqlite3
from telegram import Update, ReplyKeyboardMarkup, KeyboardButton, InlineKeyboardMarkup, InlineKeyboardButton
from telegram.ext import (
    Application, CommandHandler, MessageHandler,
    CallbackQueryHandler, ContextTypes, filters, ConversationHandler,
)

TOKEN = os.environ["BOT_TOKEN"]

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

cur.execute("""
CREATE TABLE IF NOT EXISTS photos (
    barcode TEXT PRIMARY KEY,
    file_id TEXT NOT NULL
)
""")

conn.commit()

# ==========================
# STATES
# ==========================

S_SEARCH = 0

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

def main_menu():
    return ReplyKeyboardMarkup(
        [[KeyboardButton("🔍 Find item")]],
        resize_keyboard=True
    )

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
Use the *Find item* button or /find `BARCODE`
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
        parse_mode="Markdown",
        reply_markup=main_menu()
    )

# ==========================
# /HELP
# ==========================

async def help_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(HELP_TEXT, parse_mode="Markdown", reply_markup=main_menu())

# ==========================
# FIND BUTTON — conversation
# ==========================

async def search_entry(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "🔍 Enter barcode or partial code:\n_(e.g. `778` shows all matches)_",
        parse_mode="Markdown"
    )
    return S_SEARCH

async def search_run(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.message.text.strip()

    cur.execute(
        """
        SELECT barcode, row_name, position
        FROM inventory
        WHERE barcode LIKE ?
        ORDER BY barcode, row_name, position
        """,
        (f"%{q}%",)
    )
    rows = cur.fetchall()

    if not rows:
        await update.message.reply_text(
            f"❌ Nothing found for `{q}`.",
            parse_mode="Markdown",
            reply_markup=main_menu()
        )
        return ConversationHandler.END

    # Group by barcode
    grouped = {}
    for barcode, row_name, position in rows:
        grouped.setdefault(barcode, []).append((row_name, position))

    # Single match — show card right away
    if len(grouped) == 1:
        barcode = list(grouped.keys())[0]
        await send_card(update.message, barcode, grouped[barcode])
        return ConversationHandler.END

    # Multiple matches — tappable list
    buttons = [
        [InlineKeyboardButton(barcode, callback_data=f"show:{barcode}")]
        for barcode in grouped
    ]
    await update.message.reply_text(
        f"🔍 *{len(grouped)}* matches for `{q}` — tap one:",
        parse_mode="Markdown",
        reply_markup=InlineKeyboardMarkup(buttons)
    )
    return ConversationHandler.END

async def send_card(msg, barcode, locs):
    loc_lines = "\n".join(
        f"  📍 *{row_name}* — коробка *{position}*"
        for row_name, position in locs
    )
    text  = f"🔍 *{barcode}*\n\n{loc_lines}"
    photo = get_photo_file_id(barcode)
    if photo:
        await msg.reply_photo(
            photo=photo,
            caption=text,
            parse_mode="Markdown",
            reply_markup=main_menu()
        )
    else:
        await msg.reply_text(text, parse_mode="Markdown", reply_markup=main_menu())

async def cb_show(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    barcode = q.data.split(":", 1)[1]
    cur.execute(
        "SELECT row_name, position FROM inventory WHERE barcode=? ORDER BY row_name, position",
        (barcode,)
    )
    locs = cur.fetchall()
    if not locs:
        await q.message.reply_text("❌ Not found.")
        return
    await send_card(q.message, barcode, locs)

async def cancel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("↩️ Cancelled.", reply_markup=main_menu())
    return ConversationHandler.END

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
    barcode  = context.args[0]
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
    groups   = context.args[1:]
    count = 0
    skipped = 0
    for position, group in enumerate(groups, start=1):
        for barcode in [x.strip() for x in group.split(",") if x.strip()]:
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
    groups   = context.args[1:]
    position = get_next_position(row_name)
    count = 0
    skipped = 0
    for group in groups:
        for barcode in [x.strip() for x in group.split(",") if x.strip()]:
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
# /ADDPHOTO
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
            f"❌ Barcode `{barcode}` not found in inventory.\nAdd it first with /add.",
            parse_mode="Markdown"
        )
        return
    context.user_data["pending_photo_barcode"] = barcode
    existing = get_photo_file_id(barcode)
    note = " _(replaces existing photo)_" if existing else ""
    await update.message.reply_text(
        f"📷 Ready! Now send the photo for `{barcode}`{note}.",
        parse_mode="Markdown"
    )

async def receive_photo(update: Update, context: ContextTypes.DEFAULT_TYPE):
    barcode = context.user_data.get("pending_photo_barcode")
    if not barcode:
        return
    file_id = update.message.photo[-1].file_id
    cur.execute(
        "INSERT INTO photos (barcode, file_id) VALUES (?, ?) "
        "ON CONFLICT(barcode) DO UPDATE SET file_id=excluded.file_id",
        (barcode, file_id)
    )
    conn.commit()
    context.user_data.pop("pending_photo_barcode", None)
    await update.message.reply_text(
        f"✅ Photo saved for `{barcode}`!\nUse `/find {barcode}` to see it.",
        parse_mode="Markdown"
    )

# ==========================
# /REMOVEPHOTO
# ==========================

async def removephoto(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if len(context.args) != 1:
        await update.message.reply_text("❌ Usage:\n`/removephoto BARCODE`", parse_mode="Markdown")
        return
    barcode = context.args[0]
    cur.execute("DELETE FROM photos WHERE barcode=?", (barcode,))
    conn.commit()
    if cur.rowcount == 0:
        await update.message.reply_text(f"⚠️ No photo found for `{barcode}`.", parse_mode="Markdown")
    else:
        await update.message.reply_text(f"🗑️ Photo removed for `{barcode}`.", parse_mode="Markdown")

# ==========================
# /FIND (command version)
# ==========================

async def find(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if len(context.args) != 1:
        await update.message.reply_text("❌ Usage:\n`/find BARCODE`", parse_mode="Markdown")
        return
    barcode = context.args[0]
    cur.execute(
        "SELECT row_name, position FROM inventory WHERE barcode=? ORDER BY row_name, position",
        (barcode,)
    )
    results = cur.fetchall()
    if not results:
        await update.message.reply_text(f"❌ Barcode `{barcode}` not found.", parse_mode="Markdown")
        return
    locs = [(row_name, position) for row_name, position in results]
    await send_card(update.message, barcode, locs)

# ==========================
# /SHOWROW
# ==========================

async def showrow(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if len(context.args) != 1:
        await update.message.reply_text("❌ Usage:\n`/showrow ROW_NAME`", parse_mode="Markdown")
        return
    row_name = context.args[0]
    cur.execute(
        "SELECT position, barcode FROM inventory WHERE row_name=? ORDER BY position",
        (row_name,)
    )
    results = cur.fetchall()
    if not results:
        await update.message.reply_text(f"❌ Row *{row_name}* not found.", parse_mode="Markdown")
        return
    positions = {}
    for position, barcode in results:
        positions.setdefault(position, []).append(barcode)
    text = f"📦 Row *{row_name}*:\n\n"
    for position in sorted(positions):
        labeled = [
            f"{b} 📷" if get_photo_file_id(b) else b
            for b in positions[position]
        ]
        text += f"`{position}.` {', '.join(labeled)}\n"
    text += "\n_📷 = has photo — use /find BARCODE to view_"
    await update.message.reply_text(text, parse_mode="Markdown")

# ==========================
# /ROWS
# ==========================

async def rows(update: Update, context: ContextTypes.DEFAULT_TYPE):
    cur.execute("SELECT row_name, COUNT(*) FROM inventory GROUP BY row_name ORDER BY row_name")
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
        await update.message.reply_text("❌ Usage:\n`/rename OLD_ROW NEW_ROW`", parse_mode="Markdown")
        return
    old_name, new_name = context.args[0], context.args[1]
    cur.execute("SELECT COUNT(*) FROM inventory WHERE row_name=?", (old_name,))
    count = cur.fetchone()[0]
    if count == 0:
        await update.message.reply_text(f"❌ Row *{old_name}* not found.", parse_mode="Markdown")
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
        await update.message.reply_text("❌ Usage:\n`/delete BARCODE ROW POSITION`", parse_mode="Markdown")
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
        await update.message.reply_text("❌ Usage:\n`/deleterow ROW_NAME`", parse_mode="Markdown")
        return
    row_name = context.args[0]
    cur.execute("SELECT COUNT(*) FROM inventory WHERE row_name=?", (row_name,))
    count = cur.fetchone()[0]
    if count == 0:
        await update.message.reply_text(f"❌ Row *{row_name}* not found.", parse_mode="Markdown")
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
app.add_handler(CommandHandler("showrow",     showrow))
app.add_handler(CommandHandler("rows",        rows))
app.add_handler(CommandHandler("rename",      rename))
app.add_handler(CommandHandler("delete",      delete))
app.add_handler(CommandHandler("deleterow",   deleterow))
app.add_handler(CommandHandler("clear",       clear))
app.add_handler(CommandHandler("find",        find))

app.add_handler(CallbackQueryHandler(cb_show, pattern=r"^show:"))

app.add_handler(ConversationHandler(
    entry_points=[
        MessageHandler(filters.Regex(r"^🔍 Find item$"), search_entry),
    ],
    states={
        S_SEARCH: [MessageHandler(filters.TEXT & ~filters.COMMAND, search_run)],
    },
    fallbacks=[CommandHandler("cancel", cancel)],
    per_user=True,
    per_chat=False,
    allow_reentry=True,
))

app.add_handler(MessageHandler(filters.PHOTO, receive_photo))

print("Bot running...")
app.run_polling()
