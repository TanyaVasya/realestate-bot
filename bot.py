"""Telegram bot entry point (long-polling worker)."""
import datetime
import html
import logging

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

    # 1) A real-estate link -> scrape + add to the database.
    url = scraper.find_listing_url(text)
    if url:
        await _process_listing(update, context, url)
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
            reply = llm.chat(clean, _today())
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


async def _process_listing(update: Update, context: ContextTypes.DEFAULT_TYPE, url: str) -> None:
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
        return

    comment = scraper.remove_urls(msg.text)
    raw = scraper.scrape(url)  # never raises; falls back to URL-only data

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

    text = _card(stored)
    if raw.get("blocked"):
        text += "\n\n<i>⚠️ Сайт не дал открыть страницу — заполнила по ссылке. Детали можно дописать в чате.</i>"
    await msg.reply_text(
        text, parse_mode=ParseMode.HTML, disable_web_page_preview=True
    )


def main() -> None:
    app = Application.builder().token(config.TELEGRAM_TOKEN).build()
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))
    log.info("Bot started (polling).")
    app.run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    main()
