import os
import re
import json
import time
import asyncio
from datetime import datetime, timezone, timedelta
import httpx
from aiohttp import web
from dotenv import load_dotenv
from pyrogram import Client, filters, idle
from pyrogram.enums import ParseMode
from pyrogram.errors import RPCError
from pyrogram.types import (
    CallbackQuery,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    Message,
)

# Load environment configuration
load_dotenv()

API_ID = int(os.getenv("API_ID", "0"))
API_HASH = os.getenv("API_HASH", "")
BOT_TOKEN = os.getenv("BOT_TOKEN", "")
ADMIN_USER_ID = int(os.getenv("ADMIN_USER_ID", "0"))
PORT = int(os.getenv("PORT", "10000"))
DEFAULT_LIMIT = int(os.getenv("MAX_TOKENS_PER_COUNTRY", "2"))

if not API_ID or not API_HASH or not BOT_TOKEN:
    raise ValueError("Missing API_ID, API_HASH, or BOT_TOKEN in environment!")

CHANNEL_USERNAME = "QbzTokens"
MESSAGE_ID = 3

# Qobuz API Configuration
QOBUZ_APP_ID = "312369995"
API_BASE = "https://www.qobuz.com/api.json/0.2/"

# Rate limiting lock & timestamp tracking
LAST_CHECK_TIMESTAMP = 0.0
RATE_LIMIT_SECONDS = 10
CHECK_INTERVAL_SECONDS = 1 * 3600  # 1 Hour
CHECK_LOCK = asyncio.Lock()

# Indian Standard Time (UTC+05:30)
IST = timezone(timedelta(hours=5, minutes=30))
VAULT_FILE = "tokens_vault.json"


# ---------------- State & Standby Queue Storage ----------------

def load_vault() -> dict:
    if os.path.exists(VAULT_FILE):
        try:
            with open(VAULT_FILE, "r") as f:
                data = json.load(f)
                if "limit" not in data:
                    data["limit"] = DEFAULT_LIMIT
                if "queue" not in data:
                    data["queue"] = {}
                if "history" not in data:
                    data["history"] = {}
                return data
        except Exception:
            pass
    return {"limit": DEFAULT_LIMIT, "queue": {}, "history": {}}


def save_vault(data: dict):
    with open(VAULT_FILE, "w") as f:
        json.dump(data, f, indent=2)


vault = load_vault()


# ---------------- Utilities & Formatters ----------------

def get_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        [
            [
                InlineKeyboardButton("🔄 Re-check Tokens", callback_data="recheck_tokens"),
                InlineKeyboardButton("🧹 Purge Dead Tokens", callback_data="purge_dead_tokens"),
            ]
        ]
    )


def country_code_to_flag(country_code: str) -> str:
    if not country_code or len(country_code) != 2 or country_code.upper() == "UNKNOWN":
        return "🌐"
    return "".join(chr(127397 + ord(c)) for c in country_code.upper())


def qobuz_headers(token: str) -> dict:
    return {
        "X-Device-Platform": "android",
        "X-Device-Model": "Pixel 3",
        "X-Device-Os-Version": "10",
        "X-Device-Manufacturer-Id": "ffffffff-5783-1f51-ffff-ffffef05ac4a",
        "X-App-Version": "5.16.1.5",
        "User-Agent": (
            "Dalvik/2.1.0 (Linux; U; Android 10; Pixel 3 Build/QP1A.190711.020) "
            "QobuzMobileAndroid/5.16.1.5-b21041415"
        ),
        "X-User-Auth-Token": token,
    }


def format_expiry(exp_raw: str) -> str:
    if not exp_raw:
        return "N/A"
    clean_date = str(exp_raw).split("T")[0].strip()
    try:
        dt = datetime.strptime(clean_date, "%Y-%m-%d")
        return dt.strftime("%d %b %Y")
    except Exception:
        return clean_date


def get_current_ist_timestamp() -> str:
    now_ist = datetime.now(IST)
    return now_ist.strftime("%d %b %Y | %I:%M %p IST")


def format_duration(seconds: float) -> str:
    total_seconds = int(max(0, seconds))
    days = total_seconds // 86400
    hours = (total_seconds % 86400) // 3600
    minutes = (total_seconds % 3600) // 60

    parts = []
    if days > 0:
        parts.append(f"{days}d")
    if hours > 0 or days > 0:
        parts.append(f"{hours}h")
    parts.append(f"{minutes}m")
    return " ".join(parts) if parts else "0m"


async def notify_admin_token_died(app: Client, token: str, country: str, status: str, first_seen: float):
    if not ADMIN_USER_ID:
        print("[-] Notification skipped: ADMIN_USER_ID not configured!")
        return

    now_ts = time.time()
    lifespan = format_duration(now_ts - first_seen)
    posted_date_str = datetime.fromtimestamp(first_seen, IST).strftime("%d %b %Y | %I:%M %p IST")
    flag = country_code_to_flag(country)

    text = (
        f"🚨 <b>Token Deceased Alert!</b>\n\n"
        f"• <b>Country:</b> {country} {flag}\n"
        f"• <b>Status:</b> <b>{status}</b>\n"
        f"• <b>Survived For:</b> <code>{lifespan}</code>\n"
        f"• <b>First Posted:</b> <code>{posted_date_str}</code>\n\n"
        f"<b>Token:</b>\n"
        f"<blockquote><code>{token}</code></blockquote>"
    )

    try:
        await app.send_message(chat_id=int(ADMIN_USER_ID), text=text, parse_mode=ParseMode.HTML)
        print(f"[+] Death notification delivered to admin ({ADMIN_USER_ID}) for {country} token!")
    except RPCError as e:
        print(f"[-] Telegram RPC Error sending DM to {ADMIN_USER_ID}: {e.MESSAGE or e}")
    except Exception as e:
        print(f"[-] Failed to send DM notification to admin: {e}")


# ---------------- Qobuz API Validator ----------------

async def check_qobuz_token(client: httpx.AsyncClient, token: str) -> tuple[str, str, str, bool]:
    params = {"app_id": QOBUZ_APP_ID, "user_auth_token": token}
    try:
        resp = await client.get(
            f"{API_BASE}user/get",
            params=params,
            headers=qobuz_headers(token),
            timeout=10,
        )
        try:
            data = resp.json()
        except Exception:
            data = {}

        if not isinstance(data, dict):
            data = {}

        user_obj = data.get("user") if isinstance(data.get("user"), dict) else data
        raw_country = user_obj.get("country_code") or data.get("country_code")
        detected_country = str(raw_country).upper() if raw_country else "UNKNOWN"

        if resp.status_code == 403 or data.get("error_code") == "USER_BLOCKED":
            return "🚫 [BLOCKED]", "Blocked", detected_country, False

        if resp.status_code != 200:
            return "❌ [INVALID]", "N/A", detected_country, False

        credential = user_obj.get("credential") if isinstance(user_obj.get("credential"), dict) else {}
        subscription = user_obj.get("subscription") if isinstance(user_obj.get("subscription"), dict) else {}
        parameters = credential.get("parameters") if isinstance(credential.get("parameters"), dict) else {}
        is_streamable = bool(parameters)

        exp_date_raw = (
            subscription.get("end_date")
            or subscription.get("expiration_date")
            or credential.get("end_date")
        )
        formatted_exp = format_expiry(exp_date_raw)

        is_expired = False
        if exp_date_raw:
            try:
                exp_date = datetime.strptime(str(exp_date_raw).split("T")[0], "%Y-%m-%d")
                if exp_date < datetime.now():
                    is_expired = True
            except Exception:
                pass

        if is_streamable and not is_expired:
            return "✅ [ACTIVE - Studio]", formatted_exp, detected_country, True
        elif is_expired or (exp_date_raw and not is_streamable):
            return "⚠️ [EXPIRED]", formatted_exp, detected_country, False
        else:
            return "⚪ [FREE TIER]", formatted_exp, detected_country, False

    except httpx.RequestError:
        return "⚠️ [CHECK FAILED]", "N/A", "UNKNOWN", False


# ---------------- Core Synchronizer ----------------

async def update_token_post(app: Client, purge_dead: bool = False, incoming_tokens: list = None) -> dict:
    global vault
    print(f"[*] Fetching post {MESSAGE_ID} from @{CHANNEL_USERNAME} (Purge: {purge_dead})...")
    msg: Message = await app.get_messages(CHANNEL_USERNAME, MESSAGE_ID)

    if not msg or not msg.text:
        print("[-] Could not retrieve message content.")
        return {"status": "error", "message": "Failed to fetch post"}

    content = msg.text
    lines = content.split("\n")

    header_pattern = re.compile(r"^(\d+)\.\s+([A-Z]{2}|UNKNOWN)\s+([\U0001F1E6-\U0001F1FF]{2}|🌐)", re.IGNORECASE)
    token_pattern = re.compile(r"^[A-Za-z0-9_-]{70,}$")

    existing_tokens = []
    seen_tokens = set()
    current_country = "UNKNOWN"
    title = lines[0].strip() if lines else "Qobuz Studio 2M Trial Tokens ✨"
    unformatted_loose_tokens = incoming_tokens or []

    # 1. Parse tokens from existing channel post
    for line in lines[1:]:
        stripped = line.strip()
        if not stripped:
            continue

        if any(marker in stripped.lower() for marker in [
            "user id:", "⚠️ must use", "app_id:", "enjoy!", "last checked:", "automatically"
        ]):
            continue

        if stripped.lower().startswith("exp:") or stripped.lower().startswith("status:"):
            continue

        match_header = header_pattern.match(stripped)
        if match_header:
            current_country = match_header.group(2).upper()
            continue

        if token_pattern.match(stripped):
            if stripped not in seen_tokens:
                seen_tokens.add(stripped)
                if current_country != "UNKNOWN":
                    existing_tokens.append({"token": stripped, "assigned_country": current_country})
                else:
                    unformatted_loose_tokens.append(stripped)

    max_limit = vault.get("limit", DEFAULT_LIMIT)
    standby_queue = vault.get("queue", {})
    history = vault.get("history", {})

    now_ts = time.time()
    print(f"[*] Validating {len(existing_tokens)} tokens from channel post...")
    country_groups = {}

    async with httpx.AsyncClient() as http_client:
        for item in existing_tokens:
            token = item["token"]
            status, expiry, detected_country, is_active = await check_qobuz_token(http_client, token)
            country = detected_country if detected_country != "UNKNOWN" else item["assigned_country"]

            # Initialize history record if token wasn't seen before
            if token not in history:
                history[token] = {
                    "first_seen": now_ts,
                    "country": country,
                    "last_status": status,
                    "is_active": is_active,
                    "notified_death": False,
                }

            token_meta = history[token]

            # Trigger death notification if transitioning to inactive
            if not is_active and not token_meta.get("notified_death", False):
                await notify_admin_token_died(
                    app=app,
                    token=token,
                    country=country,
                    status=status,
                    first_seen=token_meta.get("first_seen", now_ts),
                )
                token_meta["notified_death"] = True

            token_meta["is_active"] = is_active
            token_meta["last_status"] = status
            token_meta["country"] = country

            # Skip dead tokens if purge mode is active
            if purge_dead and not is_active:
                print(f"[x] Purging dead token ({status}): {token[:12]}...")
                continue

            if country not in country_groups:
                country_groups[country] = []

            country_groups[country].append({
                "country": country,
                "token": token,
                "status": status,
                "expiry": expiry,
                "is_active": is_active,
            })

        # Process incoming/unformatted tokens
        for new_token in unformatted_loose_tokens:
            if new_token in seen_tokens:
                continue
            seen_tokens.add(new_token)
            status, expiry, detected_country, is_active = await check_qobuz_token(http_client, new_token)
            if not is_active:
                print(f"[-] Dropped inactive loose token: {status}")
                continue

            country = detected_country
            if new_token not in history:
                history[new_token] = {
                    "first_seen": now_ts,
                    "country": country,
                    "last_status": status,
                    "is_active": True,
                    "notified_death": False,
                }

            if country not in country_groups:
                country_groups[country] = []

            active_in_group = [t for t in country_groups[country] if t["is_active"]]
            if len(active_in_group) < max_limit:
                country_groups[country].append({
                    "country": country,
                    "token": new_token,
                    "status": status,
                    "expiry": expiry,
                    "is_active": True,
                })
                print(f"[+] Added active token directly to post for {country}")
            else:
                if country not in standby_queue:
                    standby_queue[country] = []
                if new_token not in standby_queue[country]:
                    standby_queue[country].append(new_token)
                print(f"[~] Quota reached for {country} ({len(active_in_group)}/{max_limit}). Queued to standby.")

        # 2. Replenish dead slots from Standby Queue
        for country, tokens in country_groups.items():
            active_count = sum(1 for t in tokens if t["is_active"])
            slots_needed = max_limit - active_count

            if slots_needed > 0 and country in standby_queue and standby_queue[country]:
                print(f"[*] Attempting to replenish {slots_needed} slot(s) for {country} from standby...")
                replenished = []
                remaining_in_queue = []

                for queued_token in standby_queue[country]:
                    if slots_needed > 0:
                        q_status, q_expiry, _, q_active = await check_qobuz_token(http_client, queued_token)
                        if q_active:
                            if queued_token not in history:
                                history[queued_token] = {
                                    "first_seen": time.time(),
                                    "country": country,
                                    "last_status": q_status,
                                    "is_active": True,
                                    "notified_death": False,
                                }

                            replenished.append({
                                "country": country,
                                "token": queued_token,
                                "status": q_status,
                                "expiry": q_expiry,
                                "is_active": True,
                            })
                            slots_needed -= 1
                        else:
                            print(f"[x] Discarded dead token from standby: {queued_token[:12]}...")
                    else:
                        remaining_in_queue.append(queued_token)

                tokens.extend(replenished)
                standby_queue[country] = remaining_in_queue

    # Save persistence updates
    vault["queue"] = standby_queue
    vault["history"] = history
    save_vault(vault)

    # 3. Build formatted message
    output_parts = [f"<b>{title}</b>\n"]

    for idx, (cc, tokens) in enumerate(sorted(country_groups.items()), 1):
        if not tokens:
            continue
        flag = country_code_to_flag(cc)
        output_parts.append(f"<b><i>{idx}. {cc} {flag}</i></b>")

        for t in tokens:
            token_block = f"<blockquote><code>{t['token']}</code></blockquote>"
            status_block = f"<blockquote>Status: <b>{t['status']}</b></blockquote>"
            exp_block = f"<blockquote>Exp: <b>{t['expiry']}</b></blockquote>"
            output_parts.append(f"{token_block}\n{status_block}\n{exp_block}\n")

    user_id_block = "<blockquote><b>User ID:</b> <code>14232934</code></blockquote>"
    app_keys_block = (
        "<blockquote>"
        "⚠️ <b>must use</b>\n"
        "app_id: <code>312369995</code>\n"
        "&\n"
        "app_secret: <code>e79f8b9be485692b0e5f9dd895826368</code>"
        "</blockquote>"
    )

    last_checked_timestamp = get_current_ist_timestamp()
    last_check_block = f"<blockquote>☑️ Last Checked: <b>{last_checked_timestamp}</b></blockquote>"
    auto_check_note_block = "<blockquote><i>Note: The bot automatically checks and updates each token's status every hour.</i></blockquote>"

    footer_text = (
        f"{user_id_block}\n"
        f"{app_keys_block}\n"
        f"{last_check_block}\n"
        f"{auto_check_note_block}\n\n"
        "Enjoy! 😊🥂"
    )
    output_parts.append(footer_text)

    final_formatted_message = "\n".join(output_parts)

    print("[*] Updating channel message...")
    await app.edit_message_text(
        chat_id=CHANNEL_USERNAME,
        message_id=MESSAGE_ID,
        text=final_formatted_message,
        parse_mode=ParseMode.HTML,
        reply_markup=get_keyboard(),
        disable_web_page_preview=True,
    )
    print(f"[+] Channel post successfully updated at {last_checked_timestamp}!")
    return {"status": "ok", "timestamp": last_checked_timestamp}


# ---------------- Telegram Bot Instance & DM Handlers ----------------

app = Client(
    "qobuz_token_checker",
    api_id=API_ID,
    api_hash=API_HASH,
    bot_token=BOT_TOKEN,
    in_memory=True,
)


def admin_only_filter(_, __, message: Message):
    return message.from_user and message.from_user.id == ADMIN_USER_ID


admin_filter = filters.create(admin_only_filter)


@app.on_message(filters.private & admin_filter & filters.command("start"))
async def dm_start_cmd(_, message: Message):
    limit = vault.get("limit", DEFAULT_LIMIT)
    text = (
        "👑 <b>Qobuz Token Checker Admin Console</b>\n\n"
        f"• <b>Max tokens per region:</b> <code>{limit}</code>\n\n"
        "<b>Available Commands:</b>\n"
        "• <code>/recheck</code> - Force refresh of channel post & queue\n"
        "• <code>/purge</code> - Remove all expired/invalid tokens from post\n"
        "• <code>/limit &lt;number&gt;</code> - Change max tokens shown per region\n"
        "• <code>/queue</code> - View tokens stored in standby\n\n"
        "💡 <i>To add new tokens: send or paste raw tokens directly into this chat!</i>"
    )
    await message.reply_text(text, parse_mode=ParseMode.HTML)


@app.on_message(filters.private & admin_filter & filters.command("limit"))
async def dm_set_limit(_, message: Message):
    global vault
    parts = message.text.split()
    if len(parts) != 2 or not parts[1].isdigit():
        await message.reply_text("⚠️ Usage: <code>/limit 2</code> (must be a number)", parse_mode=ParseMode.HTML)
        return

    new_limit = int(parts[1])
    if new_limit < 1:
        await message.reply_text("⚠️ Limit must be at least 1.")
        return

    vault["limit"] = new_limit
    save_vault(vault)
    await message.reply_text(f"✅ Max tokens per country set to: <b>{new_limit}</b>", parse_mode=ParseMode.HTML)


@app.on_message(filters.private & admin_filter & filters.command("queue"))
async def dm_queue_cmd(_, message: Message):
    queue = vault.get("queue", {})
    total = sum(len(v) for v in queue.values())
    if not total:
        await message.reply_text("📦 Standby Queue is empty.", parse_mode=ParseMode.HTML)
        return

    report = [f"📦 <b>Standby Tokens Queue ({total} total):</b>\n"]
    for cc, tokens in queue.items():
        if tokens:
            flag = country_code_to_flag(cc)
            report.append(f"• <b>{cc} {flag}:</b> {len(tokens)} token(s) waiting")

    await message.reply_text("\n".join(report), parse_mode=ParseMode.HTML)


@app.on_message(filters.private & admin_filter & filters.command("recheck"))
async def dm_recheck_cmd(client: Client, message: Message):
    global LAST_CHECK_TIMESTAMP
    if CHECK_LOCK.locked():
        await message.reply_text("⏳ Check in progress. Please wait...")
        return

    wait_msg = await message.reply_text("🔄 Checking tokens & updating post...")
    async with CHECK_LOCK:
        LAST_CHECK_TIMESTAMP = time.time()
        try:
            res = await update_token_post(client, purge_dead=False)
            await wait_msg.edit_text(f"✅ Updated post successfully at {res.get('timestamp')}")
        except Exception as e:
            await wait_msg.edit_text(f"❌ Failed: {e}")


@app.on_message(filters.private & admin_filter & filters.command("purge"))
async def dm_purge_cmd(client: Client, message: Message):
    global LAST_CHECK_TIMESTAMP
    if CHECK_LOCK.locked():
        await message.reply_text("⏳ A check is currently running. Please wait...")
        return

    wait_msg = await message.reply_text("🧹 Purging dead tokens & filling from standby...")
    async with CHECK_LOCK:
        LAST_CHECK_TIMESTAMP = time.time()
        try:
            res = await update_token_post(client, purge_dead=True)
            await wait_msg.edit_text(f"✅ Post purged & updated at {res.get('timestamp')}")
        except Exception as e:
            await wait_msg.edit_text(f"❌ Failed: {e}")


@app.on_message(filters.private & admin_filter & filters.text)
async def dm_add_tokens_handler(client: Client, message: Message):
    global LAST_CHECK_TIMESTAMP
    token_pattern = re.compile(r"[A-Za-z0-9_-]{70,}")
    found_tokens = token_pattern.findall(message.text)

    if not found_tokens:
        return

    status_msg = await message.reply_text(f"🔍 Found {len(found_tokens)} token(s). Validating...")

    async with CHECK_LOCK:
        LAST_CHECK_TIMESTAMP = time.time()
        try:
            res = await update_token_post(client, purge_dead=False, incoming_tokens=found_tokens)
            await status_msg.edit_text(
                f"✅ Done!\nPost updated & regional limits applied.\nOverflow tokens (if any) saved to standby queue.\n\n"
                f"🕒 Updated at: <b>{res.get('timestamp')}</b>",
                parse_mode=ParseMode.HTML,
            )
        except Exception as e:
            await status_msg.edit_text(f"❌ Error updating: {e}")


# ---------------- Channel Button Callback Handler ----------------

@app.on_callback_query()
async def callback_handler(client: Client, callback: CallbackQuery):
    global LAST_CHECK_TIMESTAMP

    if ADMIN_USER_ID and callback.from_user.id != ADMIN_USER_ID:
        if callback.data == "purge_dead_tokens":
            msg = "⛔ Access denied: Only bot admin can purge tokens."
        else:
            msg = "⛔ Access denied: Only bot admin can trigger re-checks."
        await callback.answer(msg, show_alert=True)
        return

    chat = callback.message.chat if callback.message else None
    if not chat or chat.username != CHANNEL_USERNAME or callback.message.id != MESSAGE_ID:
        await callback.answer("Unauthorized action.", show_alert=True)
        return

    if callback.data in ("recheck_tokens", "purge_dead_tokens"):
        current_time = time.time()
        time_diff = current_time - LAST_CHECK_TIMESTAMP

        if time_diff < RATE_LIMIT_SECONDS:
            remaining = int(RATE_LIMIT_SECONDS - time_diff)
            await callback.answer(f"⏳ Please wait {remaining}s before checking again.", show_alert=True)
            return

        if CHECK_LOCK.locked():
            await callback.answer("⏳ A check is currently running. Please wait...", show_alert=True)
            return

        purge = callback.data == "purge_dead_tokens"
        action_msg = "🧹 Purging dead tokens & updating..." if purge else "🔄 Checking tokens now..."

        async with CHECK_LOCK:
            LAST_CHECK_TIMESTAMP = time.time()
            await callback.answer(action_msg, show_alert=False)
            try:
                await update_token_post(client, purge_dead=purge)
            except Exception as e:
                print(f"[-] Callback update failed: {e}")


# ---------------- Hourly Background Scheduler ----------------

async def scheduler_loop(client: Client):
    global LAST_CHECK_TIMESTAMP
    while True:
        current_ts = time.time()
        elapsed = current_ts - LAST_CHECK_TIMESTAMP
        if elapsed >= CHECK_INTERVAL_SECONDS:
            print("[*] 1-Hour interval elapsed. Triggering automatic background token check...")
            async with CHECK_LOCK:
                LAST_CHECK_TIMESTAMP = time.time()
                try:
                    await update_token_post(client, purge_dead=False)
                except Exception as e:
                    print(f"[-] Scheduled auto-check failed: {e}")

        await asyncio.sleep(60)


# ---------------- Dummy Web Server for Render ----------------

async def handle_ping(request):
    return web.Response(text="Qobuz Token Bot is running OK!")


async def start_web_server():
    server = web.Application()
    server.router.add_get("/", handle_ping)
    server.router.add_get("/health", handle_ping)
    runner = web.AppRunner(server)
    await runner.setup()
    site = web.TCPSite(runner, "0.0.0.0", PORT)
    await site.start()
    print(f"[+] Web server listening on port {PORT}")


# ---------------- Main Entry Point ----------------

async def main():
    global LAST_CHECK_TIMESTAMP

    await start_web_server()
    await app.start()
    print("[+] Bot started. Executing initial token check...")

    try:
        async with CHECK_LOCK:
            LAST_CHECK_TIMESTAMP = time.time()
            await update_token_post(app, purge_dead=False)
    except RPCError as e:
        print(f"[-] Telegram RPC Error: {e.MESSAGE or e}")
    except Exception as e:
        print(f"[-] Initial check error: {e}")

    asyncio.create_task(scheduler_loop(app))
    print("[+] Scheduler, Lifespan Monitor & DM Listeners active. Bot is ready!")
    await idle()
    await app.stop()


if __name__ == "__main__":
    app.run(main())
