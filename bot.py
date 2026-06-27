"""Telegram bot entry point (long-polling worker)."""
import asyncio
import datetime
import html
import json
import logging
import os
from zoneinfo import ZoneInfo

from telegram import Update
from telegram.constants import ChatAction, ParseMode
from telegram.ext import Application, ContextTypes, MessageHandler, filters

import config
import llm
import scraper
import sheets

logging.basicConfig(
    format="%(asctime)s %(levelname)s %(name)s: %(message)s", level=logging.INFO
)
log = logging.getLogger("realestate-bot")

# Remembers the last listing added per chat, so a follow-up message like
# "requested a viewing" can be attached to it as a note.
LAST_LISTING: dict[int, dict] = {}

# Only one real-Chrome fetch at a time (shared profile + gentler on the site).
SCRAPE_LOCK = asyncio.Lock()

# When the daily auto-check runs and where to report it.
DAILY_CHECK_TIME = datetime.time(hour=9, minute=0, tzinfo=ZoneInfo("Australia/Sydney"))
CHATS_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "known_chats.json")


def _load_chats() -> set[int]:
    try:
        with open(CHATS_FILE) as f:
            return set(json.load(f))
    except (OSError, ValueError):
        return set()


def _remember_chat(chat_id: int) -> None:
    chats = _load_chats()
    if chat_id not in chats:
        chats.add(chat_id)
        with open(CHATS_FILE, "w") as f:
            json.dump(sorted(chats), f)


def _today() -> str:
    return datetime.date.today().isoformat()


def _card(rec: dict) -> str:
    """Format a stored listing as a clean HTML Telegram message."""
    def esc(key):
        return html.escape(str(rec.get(key, "")).strip())

    # Title: address if we have it, else "Type in Suburb", else suburb.
    addr, suburb, ptype = esc("address"), esc("suburb"), esc("property_type")
    if addr:
        title = addr
    elif ptype and suburb:
        title = f"{ptype} · {suburb}"
    else:
        title = suburb or "Объявление"

    lines = [f"🏠 <b>#{rec['id']} · {title}</b>"]

    # Show suburb on its own line only if it isn't already the title.
    if suburb and title != suburb and suburb not in title:
        lines.append(f"📍 {suburb}")

    # Compact metrics row: only the parts we actually have.
    metrics = []
    if esc("price"):
        metrics.append(f"💰 {esc('price')}")
    if esc("bedrooms"):
        metrics.append(f"🛏 {esc('bedrooms')}")
    if esc("bathrooms"):
        metrics.append(f"🛁 {esc('bathrooms')}")
    if esc("parking"):
        metrics.append(f"🚗 {esc('parking')}")
    if metrics:
        lines.append("  ".join(metrics))

    if esc("inspection"):
        lines.append(f"📅 {esc('inspection')}")
    if esc("agent"):
        lines.append(f"🧑‍💼 {esc('agent')}")
    if esc("features"):
        lines.append(f"✨ {esc('features')}")

    # Status + a short clickable link (strip the long campaign query string).
    clean_url = rec.get("url", "").split("?")[0]
    status = esc("status") or "new"
    link = f'<a href="{html.escape(clean_url)}">Открыть объявление</a>' if clean_url else ""
    lines.append(f"📌 {status}   {link}".strip())

    return "\n".join(lines)


async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    msg = update.effective_message
    if not msg or not msg.text:
        return

    text = msg.text
    chat = update.effective_chat
    _remember_chat(chat.id)  # so the daily auto-check knows where to report

    # 1) One or more real-estate links -> scrape each + add to the database.
    if scraper.find_listing_urls(text):
        await _process_message(update, context, text)
        return

    # 2) Addressed to the bot (mention / reply / DM) -> full conversation.
    bot_username = (await context.bot.get_me()).username
    mentioned = bot_username and f"@{bot_username}".lower() in text.lower()
    replied_to_bot = (
        msg.reply_to_message
        and msg.reply_to_message.from_user
        and msg.reply_to_message.from_user.is_bot
    )
    is_private = chat.type == "private"

    if mentioned or replied_to_bot or is_private:
        clean = text.replace(f"@{bot_username}", "").strip() if bot_username else text
        await context.bot.send_chat_action(chat.id, ChatAction.TYPING)
        try:
            reply = await asyncio.to_thread(llm.chat, clean, _today())
        except Exception as exc:  # noqa: BLE001 - surface errors to the user
            log.exception("chat failed")
            reply = f"Упс, не получилось: {exc}"
        await msg.reply_text(reply or "…", disable_web_page_preview=True)
        return

    # 3) A plain message right after a shared listing -> maybe a note about it.
    last = LAST_LISTING.get(chat.id)
    if last:
        try:
            note = llm.classify_followup(text, last)
        except Exception:  # noqa: BLE001 - stay quiet on errors here
            log.exception("followup classify failed")
            return
        if note and sheets.append_note(int(last["id"]), note):
            await msg.reply_text(
                f"📝 Записала к #{last['id']}: {note}", disable_web_page_preview=True
            )


async def _process_message(update: Update, context: ContextTypes.DEFAULT_TYPE, text: str) -> None:
    """Handle a message that contains one or more listing links.

    Line-based: text on a link's own line is that listing's address/comment;
    a line with no link is a note for the most recent listing in the message.
    """
    msg = update.effective_message
    chat = update.effective_chat
    current = None  # the listing most recently added in THIS message

    for line in text.splitlines():
        urls = scraper.find_listing_urls(line)
        if urls:
            comment = scraper.remove_urls(line).strip()
            for url in urls:
                stored = await _add_listing(update, context, url, comment)
                comment = ""  # the typed comment belongs to the first link only
                if stored:
                    current = stored
        else:
            note = line.strip()
            target = current or LAST_LISTING.get(chat.id)
            if note and target and sheets.append_note(int(target["id"]), note):
                await msg.reply_text(
                    f"📝 Записала к #{target['id']}: {note}",
                    disable_web_page_preview=True,
                )


async def _add_listing(update: Update, context: ContextTypes.DEFAULT_TYPE,
                       url: str, comment: str) -> dict | None:
    """Add a single listing and reply with its card. Returns the stored record."""
    msg = update.effective_message
    chat = update.effective_chat
    await context.bot.send_chat_action(chat.id, ChatAction.TYPING)

    existing = sheets.find_by_url(url)
    if existing:
        LAST_LISTING[chat.id] = existing
        await msg.reply_text(
            f"Эта квартира уже в базе — #{existing['id']} "
            f"({existing.get('suburb') or existing.get('address') or 'без адреса'}), "
            f"статус: {existing.get('status', 'new')}.",
            disable_web_page_preview=True,
        )
        return existing

    # Runs a real Chrome under the hood, so do it off the event loop, one at a time.
    async with SCRAPE_LOCK:
        raw = await asyncio.to_thread(scraper.scrape, url)  # never raises

    # If the page was blocked and the user typed no extra info, don't ask the
    # LLM to invent fields from nothing — just use what the URL tells us.
    if raw.get("blocked") and not comment:
        fields = dict(raw.get("url_fields", {}))
    else:
        try:
            fields = llm.extract_listing(raw, comment)
        except Exception:  # noqa: BLE001
            log.exception("extract failed")
            fields = {**raw.get("url_fields", {}), "notes": comment}

    record = {
        **fields,
        "date_added": _today(),
        "added_by": (msg.from_user.first_name if msg.from_user else ""),
        "status": "new",
        "url": url,
        "source": raw.get("source", ""),
        "image_url": raw.get("image_url", ""),
        "viewed": "no",
    }
    stored = sheets.add(record)
    LAST_LISTING[chat.id] = stored

    card = _card(stored)
    if raw.get("blocked"):
        card += "\n\n<i>⚠️ Сайт не дал открыть страницу — заполнила по ссылке. Детали можно дописать в чате.</i>"
    await msg.reply_text(card, parse_mode=ParseMode.HTML, disable_web_page_preview=True)
    return stored


# Fields worth reporting when they change during the daily check.
_WATCHED = [("price", "💰 Цена"), ("inspection", "📅 Инспекция")]


def _recheck_all() -> list[tuple[dict, dict]]:
    """Re-scrape every active listing; update changed fields. Runs in a thread.

    Returns a list of (listing_row, {field: (old, new)}) for reported changes.
    """
    changes = []
    for row in sheets.get_all():
        if str(row.get("status", "")).lower() == "rejected" or not row.get("url"):
            continue
        try:
            raw = scraper.scrape(row["url"])
            if raw.get("blocked"):
                continue
            fields = llm.extract_listing(raw)
        except Exception:  # noqa: BLE001 - skip this one, keep going
            log.exception("recheck failed for #%s", row.get("id"))
            continue

        # Update any changed descriptive field; report the watched ones.
        updates, reported = {}, {}
        for key, value in fields.items():
            if value and key in sheets.EDITABLE and value != str(row.get(key, "")):
                updates[key] = value
        for key, _label in _WATCHED:
            if key in updates:
                reported[key] = (str(row.get(key, "")), updates[key])
        if updates:
            sheets.update(int(row["id"]), updates)
        if reported:
            changes.append((row, reported))
    return changes


async def daily_check(context: ContextTypes.DEFAULT_TYPE) -> None:
    """Daily: re-check active listings and report price/inspection changes."""
    chats = _load_chats()
    if not chats:
        return
    log.info("daily check started")
    changes = await asyncio.to_thread(_recheck_all)
    if not changes:
        log.info("daily check: no changes")
        return  # stay quiet on no-change days

    lines = ["🔄 <b>Ежедневная проверка — что изменилось:</b>", ""]
    for row, reported in changes:
        name = html.escape(row.get("address") or row.get("suburb") or "объявление")
        lines.append(f"🏠 #{row['id']} · {name}")
        for key, label in _WATCHED:
            if key in reported:
                old, new = reported[key]
                old = html.escape(old or "—")
                lines.append(f"{label}: {old} → <b>{html.escape(new)}</b>")
        lines.append("")
    message = "\n".join(lines).strip()

    for chat_id in chats:
        try:
            await context.bot.send_message(
                chat_id, message, parse_mode=ParseMode.HTML, disable_web_page_preview=True
            )
        except Exception:  # noqa: BLE001
            log.exception("failed to post daily check to %s", chat_id)


def main() -> None:
    app = Application.builder().token(config.TELEGRAM_TOKEN).build()
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))
    app.job_queue.run_daily(daily_check, time=DAILY_CHECK_TIME, name="daily_check")
    log.info("Bot started (polling). Daily check at %s.", DAILY_CHECK_TIME)
    app.run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    main()
