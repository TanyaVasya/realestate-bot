"""Claude under the hood: extract listing fields and run the chat interface."""
import json

from anthropic import Anthropic

import config
import scraper
import sheets

client = Anthropic(api_key=config.ANTHROPIC_API_KEY)

# ---- Listing extraction -----------------------------------------------------

EXTRACT_TOOL = {
    "name": "save_listing",
    "description": "Save the structured details extracted from a property page.",
    "input_schema": {
        "type": "object",
        "properties": {
            "address": {"type": "string", "description": "Full street address"},
            "suburb": {"type": "string"},
            "price": {"type": "string", "description": "Price or rent as advertised, e.g. '$750 per week' or '$1.2M'"},
            "bedrooms": {"type": "string"},
            "bathrooms": {"type": "string"},
            "parking": {"type": "string"},
            "property_type": {"type": "string", "description": "Apartment, House, Townhouse, etc."},
            "land_size": {"type": "string"},
            "inspection": {"type": "string", "description": "Inspection / open-home times if listed"},
            "agent": {"type": "string", "description": "Agency or agent name"},
            "features": {"type": "string", "description": "Short comma-separated list of notable features"},
            "market_status": {"type": "string", "description": "Sale/lease state of the property as shown on the page: one of 'available', 'under offer', 'sold', 'leased', 'deposit taken'. Use 'sold' if the URL contains /sold/ or the page says Sold; 'leased' for a leased rental. Default 'available' if it is clearly still listed."},
            "notes": {"type": "string", "description": "Any comment the user typed alongside the link (e.g. 'requested an inspection', 'asked to view tomorrow 5:30'). Empty if none."},
        },
        "required": ["address"],
    },
}


def extract_listing(raw: dict, comment: str = "") -> dict:
    """Turn raw scraped signals (+ the user's typed comment) into clean fields."""
    payload = json.dumps(
        {
            "source": raw.get("source"),
            "from_url": raw.get("url_fields", {}),
            "user_comment": comment,
            "title": raw.get("title"),
            "og": raw.get("og"),
            "json_ld": raw.get("json_ld"),
            "text": raw.get("text"),
            "page_blocked": raw.get("blocked", False),
        },
        ensure_ascii=False,
    )
    resp = client.messages.create(
        model=config.MODEL,
        max_tokens=1024,
        tools=[EXTRACT_TOOL],
        tool_choice={"type": "tool", "name": "save_listing"},
        messages=[{
            "role": "user",
            "content": (
                "Extract the real-estate listing details from the data below. "
                "Use JSON-LD and OpenGraph first; fall back to the page text. "
                "If the page was blocked, rely on 'from_url' and 'user_comment'. "
                "Prefer an address the user typed in 'user_comment' over a guess. "
                "Put any human remark from 'user_comment' into the notes field. "
                "Leave a field empty if unknown; do not invent values.\n\n" + payload
            ),
        }],
    )
    placeholders = {"n/a", "na", "none", "-", "?", "не указано", "not specified"}
    for block in resp.content:
        if block.type == "tool_use":
            fields = {}
            for k, v in block.input.items():
                v = (v or "").strip()
                low = v.lower()
                fields[k] = "" if (low in placeholders or "unknown" in low) else v
            # Backfill from the URL when the model left things blank.
            for key, val in raw.get("url_fields", {}).items():
                if key in ("property_type", "suburb") and not fields.get(key):
                    fields[key] = val
            return fields
    return {}


# ---- Follow-up note classification ------------------------------------------

FOLLOWUP_TOOL = {
    "name": "classify",
    "description": "Decide whether a chat message is a note about the most recently shared apartment.",
    "input_schema": {
        "type": "object",
        "properties": {
            "is_note": {
                "type": "boolean",
                "description": "True only if the message is clearly a remark/update about that apartment (e.g. 'requested a viewing', 'too far', 'asked to see it tomorrow'). False for unrelated chit-chat or questions to the bot.",
            },
            "note": {"type": "string", "description": "The note text to store, cleaned up. Empty if is_note is false."},
        },
        "required": ["is_note", "note"],
    },
}


def classify_followup(text: str, listing: dict) -> str | None:
    """Return cleaned note text if the message is a note about `listing`, else None."""
    ctx = json.dumps(
        {k: listing.get(k) for k in ("id", "address", "suburb", "url")},
        ensure_ascii=False,
    )
    resp = client.messages.create(
        model=config.MODEL,
        max_tokens=400,
        tools=[FOLLOWUP_TOOL],
        tool_choice={"type": "tool", "name": "classify"},
        messages=[{
            "role": "user",
            "content": (
                f"The apartment most recently shared in the chat is:\n{ctx}\n\n"
                f"A new message just arrived: {text!r}\n\n"
                "Is this a note/update about that apartment? Be conservative: "
                "only say yes if it clearly refers to a property action or opinion."
            ),
        }],
    )
    for block in resp.content:
        if block.type == "tool_use" and block.input.get("is_note"):
            return block.input.get("note") or text
    return None


# ---- Conversational interface -----------------------------------------------

SYSTEM = """You are a helpful assistant living in a couple's private Telegram chat where they track rental/sale apartments they are considering.

You have access to their shared database of listings (provided below as JSON). When they ask questions ("what haven't we viewed?", "anything under $800 in Bondi?", "which ones did we rate highest?") answer concisely from that data.

LANGUAGE: always reply in Russian, regardless of the language of the listing data. Keep listing data (addresses, suburbs, feature lists) exactly as stored — do not translate them. Only switch away from Russian if the user clearly writes to you in another language.

When they want to record something ("mark #3 as viewed, we loved it 9/10", "reject the Surry Hills one", "add a note that it's near the station"), call update_listing. Status values: new, interested, shortlisted, viewed, rejected, applied. When they say they viewed a place, set viewed=yes and viewed_date to today if they give a date.

When they clearly want to remove/delete a listing from the database ("delete #3", "убери квартиру в Zetland", "remove this one"), call delete_listing. Deletion is permanent, so only do it on a clear delete request, not for "reject"/"not interested" (those are status changes via update_listing).

You CAN re-check a listing's current details on its website. When they ask to refresh/recheck a listing or whether something changed ("проверь, не изменилась ли цена у #15", "обнови данные #3", "актуальна ли инспекция?"), call refresh_listing with that id — it reopens the page and updates the stored fields. So never say you have no internet access; you can fetch a listing again via its saved link.

Be brief and friendly. Refer to listings by their id and suburb/address so it's clear which one you mean.

FORMATTING: your replies are shown in Telegram, which does NOT render Markdown. Write plain text only. Never use **, __, backticks, or # headers — they show up as literal characters. For structure use emoji and simple dashes (-) for lists. Keep it short and scannable.

LINKS: each listing has a "url" field. When you mention or list specific properties, include the link so it is clickable (a plain URL on its own is clickable in Telegram). Use the clean URL without the query string — i.e. cut everything from "?" onward. Do not claim you cannot add links or edit Telegram; you can simply paste the URL."""

UPDATE_TOOL = {
    "name": "update_listing",
    "description": "Update any field of a listing in the shared database. Set only the fields the user mentions.",
    "input_schema": {
        "type": "object",
        "properties": {
            "id": {"type": "integer", "description": "The listing id to update"},
            "status": {"type": "string", "enum": ["new", "interested", "shortlisted", "viewed", "rejected", "applied"]},
            "our_rating": {"type": "string", "description": "Rating, e.g. '8/10'"},
            "viewed": {"type": "string", "enum": ["yes", "no"]},
            "viewed_date": {"type": "string"},
            "notes": {"type": "string", "description": "Free-text review/notes to set (replaces existing notes)"},
            "address": {"type": "string"},
            "suburb": {"type": "string"},
            "price": {"type": "string", "description": "e.g. '$750 per week'"},
            "bedrooms": {"type": "string"},
            "bathrooms": {"type": "string"},
            "parking": {"type": "string"},
            "property_type": {"type": "string"},
            "land_size": {"type": "string"},
            "inspection": {"type": "string", "description": "Open-home / inspection time"},
            "agent": {"type": "string"},
            "features": {"type": "string"},
        },
        "required": ["id"],
    },
}

DELETE_TOOL = {
    "name": "delete_listing",
    "description": "Permanently delete a listing from the database. Use only on a clear delete request.",
    "input_schema": {
        "type": "object",
        "properties": {
            "id": {"type": "integer", "description": "The listing id to delete"},
        },
        "required": ["id"],
    },
}

REFRESH_TOOL = {
    "name": "refresh_listing",
    "description": "Reopen a listing's webpage and update its stored details (price, inspection, beds, etc.). Use when the user wants to re-check current data.",
    "input_schema": {
        "type": "object",
        "properties": {
            "id": {"type": "integer", "description": "The listing id to refresh"},
        },
        "required": ["id"],
    },
}


def _refresh(listing_id: int) -> str:
    """Re-scrape a listing by its saved URL and update changed fields."""
    row = next((r for r in sheets.get_all() if str(r.get("id")) == str(listing_id)), None)
    if not row:
        return f"no listing with id {listing_id}"
    if not row.get("url"):
        return f"listing #{listing_id} has no saved url"
    raw = scraper.scrape(row["url"])
    if raw.get("blocked"):
        return f"could not open #{listing_id} — site blocked the request"
    fields = extract_listing(raw)
    updates = {k: v for k, v in fields.items() if v and k in sheets.EDITABLE}
    if not updates:
        return f"refreshed #{listing_id}, nothing new found"
    sheets.update(int(listing_id), updates)
    return f"refreshed #{listing_id}: " + json.dumps(updates, ensure_ascii=False)


def chat(user_text: str, today: str) -> str:
    """Answer a chat message, possibly updating the database via tools."""
    listings = sheets.get_all()
    db_json = json.dumps(listings, ensure_ascii=False)

    messages = [{
        "role": "user",
        "content": f"Today is {today}.\n\nCurrent database:\n{db_json}\n\nUser says: {user_text}",
    }]

    # Tool loop: let Claude call update_listing as many times as needed.
    for _ in range(6):
        resp = client.messages.create(
            model=config.MODEL,
            max_tokens=1024,
            system=[{
                "type": "text",
                "text": SYSTEM,
                "cache_control": {"type": "ephemeral"},
            }],
            tools=[UPDATE_TOOL, DELETE_TOOL, REFRESH_TOOL],
            messages=messages,
        )

        tool_uses = [b for b in resp.content if b.type == "tool_use"]
        if not tool_uses:
            return "".join(b.text for b in resp.content if b.type == "text").strip()

        messages.append({"role": "assistant", "content": resp.content})
        results = []
        for tu in tool_uses:
            listing_id = int(tu.input["id"])
            if tu.name == "delete_listing":
                deleted = sheets.delete(listing_id)
                content = f"deleted #{listing_id}" if deleted else f"no listing with id {listing_id}"
            elif tu.name == "refresh_listing":
                content = _refresh(listing_id)
            else:
                fields = {k: v for k, v in tu.input.items() if k != "id"}
                ok = sheets.update(listing_id, fields)
                content = "updated" if ok else f"no listing with id {listing_id}"
            results.append({
                "type": "tool_result",
                "tool_use_id": tu.id,
                "content": content,
            })
        messages.append({"role": "user", "content": results})

    return "Готово (но что-то заняло слишком много шагов, проверь базу)."
