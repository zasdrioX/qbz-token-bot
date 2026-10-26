import os
import re
import time
import asyncio
from datetime import datetime, timezone, timedelta
import httpx
from aiohttp import web
from dotenv import load_dotenv
from pyrogram import Client, idle
from pyrogram.enums import ParseMode
from pyrogram.errors import RPCError
from pyrogram.types import (
    CallbackQuery,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    Message,
)

# Load environment variables
load_dotenv()

API_ID = int(os.getenv("API_ID", "0"))
API_HASH = os.getenv("API_HASH", "")
BOT_TOKEN = os.getenv("BOT_TOKEN", "")
ADMIN_USER_ID = int(os.getenv("ADMIN_USER_ID", "0"))
PORT = int(os.getenv("PORT", "10000"))

if not API_ID or not API_HASH or not BOT_TOKEN:
    raise ValueError("Missing API_ID, API_HASH, or BOT_TOKEN in environment!")

CHANNEL_USERNAME = "QbzTokens"
MESSAGE_ID = 3

# Qobuz App Configuration
QOBUZ_APP_ID = "312369995"
API_BASE = "https://www.qobuz.com/api.json/0.2/"

# Rate limiting lock & timestamp
LAST_CHECK_TIMESTAMP = 0.0
RATE_LIMIT_SECONDS = 10
CHECK_INTERVAL_SECONDS = 1 * 3600  # 1 Hour
CHECK_LOCK = asyncio.Lock()

# Indian Standard Time (+05:30)
IST = timezone(timedelta(hours=5, minutes=30))


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
    return now_ist.strftime("%d %b %Y | %Hh%Mm IST")


async def check_qobuz_token(client: httpx.AsyncClient, token: str) -> tuple[str, str, str, bool]:
    params = {
        "app_id": QOBUZ_APP_ID,
        "user_auth_token": token,
    }

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


async def update_token_post(app: Client, purge_dead: bool = False):
    print(f"[*] Fetching post {MESSAGE_ID} from @{CHANNEL_USERNAME} (Purge: {purge_dead})...")
    msg: Message = await app.get_messages(CHANNEL_USERNAME, MESSAGE_ID)

    if not msg or not msg.text:
        print("[-] Could not retrieve message content.")
        return

    content = msg.text
    lines = content.split("\n")

    header_pattern = re.compile(r"^(\d+)\.\s+([A-Z]{2}|UNKNOWN)\s+([\U0001F1E6-\U0001F1FF]{2}|🌐)", re.IGNORECASE)
    token_pattern = re.compile(r"^[A-Za-z0-9_-]{70,}$")

    existing_tokens = []
    seen_tokens = set()
    current_country = "UNKNOWN"
    title = lines[0].strip() if lines else "Qobuz Studio 2M Trial Tokens ✨"
    unformatted_new_tokens = []

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
                    unformatted_new_tokens.append(stripped)

    print(f"[*] Validating {len(existing_tokens)} existing tokens...")
    validated_tokens = []

    async with httpx.AsyncClient() as http_client:
        for item in existing_tokens:
            token = item["token"]
            status, expiry, detected_country, is_active = await check_qobuz_token(http_client, token)

            if purge_dead and not is_active:
                print(f"[x] Purging dead token ({status}): {token[:12]}...")
                continue

            country = detected_country if detected_country != "UNKNOWN" else item["assigned_country"]
            validated_tokens.append({
                "country": country,
                "token": token,
                "status": status,
                "expiry": expiry,
                "is_active": is_active,
            })

        if unformatted_new_tokens:
            print(f"[*] Found {len(unformatted_new_tokens)} newly pasted loose tokens...")
            for new_token in unformatted_new_tokens:
                status, expiry, detected_country, is_active = await check_qobuz_token(http_client, new_token)
                if is_active:
                    validated_tokens.append({
                        "country": detected_country,
                        "token": new_token,
                        "status": status,
                        "expiry": expiry,
                        "is_active": is_active,
                    })
                    print(f"[+] Ingested active token for {detected_country}: {new_token[:12]}...")
                else:
                    print(f"[-] Dropped inactive/invalid new token: {status}")

    country_groups = {}
    for entry in validated_tokens:
        cc = entry["country"]
        if cc not in country_groups:
            country_groups[cc] = []
        country_groups[cc].append(entry)

    output_parts = [f"<b>{title}</b>\n"]

    for idx, (cc, tokens) in enumerate(country_groups.items(), 1):
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
    print(f"[+] Message updated at {last_checked_timestamp}!")


app = Client(
    "qobuz_token_checker",
    api_id=API_ID,
    api_hash=API_HASH,
    bot_token=BOT_TOKEN,
    in_memory=True,
)


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


async def main():
    global LAST_CHECK_TIMESTAMP

    # 1. Start HTTP health server for Render
    await start_web_server()

    # 2. Start Pyrogram client
    await app.start()
    print("[+] Bot started. Executing initial check...")

    try:
        async with CHECK_LOCK:
            LAST_CHECK_TIMESTAMP = time.time()
            await update_token_post(app, purge_dead=False)
    except RPCError as e:
        print(f"[-] Telegram RPC Error: {e.MESSAGE or e}")
    except Exception as e:
        print(f"[-] Initial check error: {e}")

    # 3. Start background scheduler
    asyncio.create_task(scheduler_loop(app))

    print("[+] Scheduler active (every hour). Monitoring button callbacks...")
    await idle()
    await app.stop()


if __name__ == "__main__":
    app.run(main())
