"""Claude under the hood: extract listing fields and run the chat interface."""
import json

from anthropic import Anthropic

import config
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
    placeholders = {"unknown", "<unknown>", "n/a", "na", "none", "-", "?"}
    for block in resp.content:
        if block.type == "tool_use":
            fields = {}
            for k, v in block.input.items():
                v = (v or "").strip()
                fields[k] = "" if v.lower() in placeholders else v
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

You have access to their shared database of listings (provided below as JSON). When they ask questions ("what haven't we viewed?", "anything under $800 in Bondi?", "which ones did we rate highest?") answer concisely from that data, in the language the user wrote in.

When they want to record something ("mark #3 as viewed, we loved it 9/10", "reject the Surry Hills one", "add a note that it's near the station"), call update_listing. Status values: new, interested, shortlisted, viewed, rejected, applied. When they say they viewed a place, set viewed=yes and viewed_date to today if they give a date.

Be brief and friendly. Refer to listings by their id and suburb/address so it's clear which one you mean."""

UPDATE_TOOL = {
    "name": "update_listing",
    "description": "Update tracking fields of a listing in the shared database.",
    "input_schema": {
        "type": "object",
        "properties": {
            "id": {"type": "integer", "description": "The listing id to update"},
            "status": {"type": "string", "enum": ["new", "interested", "shortlisted", "viewed", "rejected", "applied"]},
            "our_rating": {"type": "string", "description": "Rating, e.g. '8/10'"},
            "viewed": {"type": "string", "enum": ["yes", "no"]},
            "viewed_date": {"type": "string"},
            "notes": {"type": "string", "description": "Free-text review/notes to set"},
        },
        "required": ["id"],
    },
}


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
            tools=[UPDATE_TOOL],
            messages=messages,
        )

        tool_uses = [b for b in resp.content if b.type == "tool_use"]
        if not tool_uses:
            return "".join(b.text for b in resp.content if b.type == "text").strip()

        messages.append({"role": "assistant", "content": resp.content})
        results = []
        for tu in tool_uses:
            fields = {k: v for k, v in tu.input.items() if k != "id"}
            ok = sheets.update(int(tu.input["id"]), fields)
            results.append({
                "type": "tool_result",
                "tool_use_id": tu.id,
                "content": "updated" if ok else f"no listing with id {tu.input['id']}",
            })
        messages.append({"role": "user", "content": results})

    return "Готово (но что-то заняло слишком много шагов, проверь базу)."
