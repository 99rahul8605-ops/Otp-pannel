import os
import sys
import io
import tempfile
import asyncio
import logging
import random
import time
import aiohttp
from datetime import datetime, timezone, timedelta

IST = timezone(timedelta(hours=5, minutes=30))

def now_ist():
    """Current time in IST, returned as a naive datetime so it stays
    comparable/sortable with existing datetime fields already in MongoDB."""
    return datetime.now(IST).replace(tzinfo=None)
from dotenv import load_dotenv
from telethon import TelegramClient, events, Button, functions
from telethon.sessions import StringSession
from telethon.errors import (
    SessionPasswordNeededError,
    UserNotParticipantError,
    ChatAdminRequiredError,
    ChannelPrivateError,
    AccessTokenInvalidError,
    MessageNotModifiedError
)
from motor.motor_asyncio import AsyncIOMotorClient
import qrcode
from bson import ObjectId
from account_manager import AccountManager

# ---------- .env LOAD ----------
load_dotenv()

API_ID = int(os.getenv("API_ID", "0").strip())
API_HASH = os.getenv("API_HASH", "").strip()
BOT_TOKEN = os.getenv("BOT_TOKEN", "").strip()
MONGO_URL = os.getenv("MONGO_URL", "mongodb://localhost:27017").strip()
ADMIN_IDS = [int(x.strip()) for x in os.getenv("ADMIN_IDS", "").split(",") if x.strip()]
UPI_ID = os.getenv("UPI_ID", "example@upi").strip()
PAYEE_NAME = os.getenv("PAYEE_NAME", "OTPShop").strip()
DEFAULT_PRICE = float(os.getenv("DEFAULT_PRICE", "50").strip())
REFERRAL_BONUS_PERCENT = float(os.getenv("REFERRAL_BONUS_PERCENT", "10").strip())
REFERRAL_BONUS_MAX = float(os.getenv("REFERRAL_BONUS_MAX", "5").strip())
MIN_DEPOSIT = float(os.getenv("MIN_DEPOSIT", "10").strip())

SMM_API_URL = os.getenv("SMM_API_URL", "").strip()
SMM_API_KEY = os.getenv("SMM_API_KEY", "").strip()
SMM_DEFAULT_MARKUP = 1 + float(os.getenv("DEFAULT_MARKUP_PERCENT", "20").strip()) / 100
SMM_TG_MEMBER_MARKUP = 1 + float(os.getenv("TG_MEMBER_MARKUP_PERCENT", "50").strip()) / 100

LOGS_CHANNEL_ID = os.getenv("LOGS_CHANNEL_ID", "").strip()
if LOGS_CHANNEL_ID:
    try:
        LOGS_CHANNEL_ID = int(LOGS_CHANNEL_ID)
    except ValueError:
        LOGS_CHANNEL_ID = None
        logging.warning("LOGS_CHANNEL_ID is not a valid integer, logs disabled.")
else:
    LOGS_CHANNEL_ID = None

FORCE_JOIN_SINGLE = os.getenv("FORCE_JOIN_CHAT_ID", "").strip()
FORCE_JOIN_LIST_RAW = os.getenv("FORCE_JOIN_CHAT_IDS", "").strip()
if FORCE_JOIN_LIST_RAW:
    RAW_CHAT_IDS = [x.strip() for x in FORCE_JOIN_LIST_RAW.split(",") if x.strip()]
elif FORCE_JOIN_SINGLE:
    RAW_CHAT_IDS = [FORCE_JOIN_SINGLE]
else:
    RAW_CHAT_IDS = []

if not all([API_ID, API_HASH, BOT_TOKEN, ADMIN_IDS]):
    raise ValueError("❌ .env file incomplete! Check API_ID, API_HASH, BOT_TOKEN, ADMIN_IDS")

logging.basicConfig(level=logging.INFO)

# ---------- MongoDB Setup ----------
mongo_client = AsyncIOMotorClient(MONGO_URL)
db = mongo_client['otp_bot']
accounts_col = db['accounts']
users_col = db['users']
orders_col = db['orders']
deposits_col = db['deposits']
settings_col = db['settings']
withdrawals_col = db['withdrawals']
smm_orders_col = db['smm_orders']

# ---------- BOT INSTANCE ----------
import hashlib
session_name = "bot_session_" + hashlib.md5(BOT_TOKEN.encode()).hexdigest()[:8]
bot = TelegramClient(session_name, API_ID, API_HASH)

# ---------- STATE MACHINE ----------
user_states = {}
pending_otp_requests = {}

# ---------- Bot Username Cache ----------
bot_username = None

async def get_bot_username():
    global bot_username
    if bot_username is None:
        me = await bot.get_me()
        bot_username = me.username
    return bot_username

# ---------- SETTINGS HELPERS ----------
async def get_support_link():
    setting = await settings_col.find_one({"key": "support_link"})
    if setting:
        return setting.get("value")
    return os.getenv("SUPPORT_LINK", "").strip() or None

async def set_support_link(link: str):
    await settings_col.update_one(
        {"key": "support_link"},
        {"$set": {"value": link, "updated_at": now_ist()}},
        upsert=True
    )

async def get_min_withdrawal():
    setting = await settings_col.find_one({"key": "min_withdrawal"})
    if setting:
        return float(setting.get("value", 10.0))
    return 10.0

async def set_min_withdrawal(value: float):
    await settings_col.update_one(
        {"key": "min_withdrawal"},
        {"$set": {"value": value, "updated_at": now_ist()}},
        upsert=True
    )

def _is_member_category(cat: str) -> bool:
    c = (cat or "").lower()
    return "member" in c or "subscriber" in c

async def get_smm_markup(cat: str = "") -> float:
    """Category-aware markup: Members/Subscribers get the higher TG_MEMBER_MARKUP,
    everything else gets DEFAULT_MARKUP. Admin-set overrides (via settings_col) win."""
    is_member = _is_member_category(cat)
    key = "smm_markup_member" if is_member else "smm_markup_default"
    fallback = SMM_TG_MEMBER_MARKUP if is_member else SMM_DEFAULT_MARKUP

    setting = await settings_col.find_one({"key": key})
    if setting:
        return float(setting.get("value", fallback))
    return fallback

async def set_smm_markup(value: float, member: bool = False):
    key = "smm_markup_member" if member else "smm_markup_default"
    await settings_col.update_one(
        {"key": key},
        {"$set": {"value": value, "updated_at": now_ist()}},
        upsert=True
    )

async def get_referral_bonus_percent() -> float:
    setting = await settings_col.find_one({"key": "referral_bonus_percent"})
    if setting:
        return float(setting.get("value", REFERRAL_BONUS_PERCENT))
    return REFERRAL_BONUS_PERCENT

async def get_referral_bonus_max() -> float:
    setting = await settings_col.find_one({"key": "referral_bonus_max"})
    if setting:
        return float(setting.get("value", REFERRAL_BONUS_MAX))
    return REFERRAL_BONUS_MAX

async def set_referral_bonus_percent(value: float):
    await settings_col.update_one(
        {"key": "referral_bonus_percent"},
        {"$set": {"value": value, "updated_at": now_ist()}},
        upsert=True
    )

async def set_referral_bonus_max(value: float):
    await settings_col.update_one(
        {"key": "referral_bonus_max"},
        {"$set": {"value": value, "updated_at": now_ist()}},
        upsert=True
    )

# ---------- LOGS CHANNEL HELPER ----------
async def get_display_name(user_id: int) -> str:
    """Fetch a readable 'Name (@username)' string for logs, falling back to the raw ID."""
    try:
        entity = await bot.get_entity(user_id)
        name = entity.first_name or "Unknown"
        if entity.username:
            return f"{name} (@{entity.username})"
        return name
    except Exception:
        return str(user_id)

async def log_event(text):
    if LOGS_CHANNEL_ID:
        try:
            await bot.send_message(LOGS_CHANNEL_ID, text, parse_mode="markdown")
        except Exception as e:
            logging.error(f"Failed to send log to channel: {e}")

# ---------- HELPER ----------
async def get_existing_countries():
    return await accounts_col.distinct("country", {})

# ============================================================
#  SMM PANEL INTEGRATION
# ============================================================

_smm_all_services: list = []
_smm_categorized: dict = {}
_smm_cache_time: float = 0

_usd_inr: float = 95.0
_usd_fetched: float = 0

SMM_CATS_PER_PAGE = 10
SMM_SVCS_PER_PAGE = 8

SMM_PLATFORMS = ["Telegram", "Instagram", "Facebook", "Other"]
_SMM_PLATFORM_EMOJI = {
    "Telegram": "✈️", "Instagram": "📸", "Facebook": "📘", "Other": "🌐",
}

def classify_smm_platform(cat: str) -> str:
    c = (cat or "").lower()
    if "telegram" in c or c.startswith("tg"):
        return "Telegram"
    if "instagram" in c or " ig " in f" {c} " or c.startswith("insta"):
        return "Instagram"
    if "facebook" in c or c.startswith("fb"):
        return "Facebook"
    return "Other"

def get_smm_platform_categories(platform: str) -> list:
    cats = [cat for cat in _smm_categorized.keys() if classify_smm_platform(cat) == platform]
    if platform == "Telegram":
        # Reaction services first, then alphabetical within each group
        cats.sort(key=lambda c: (0 if "reaction" in c.lower() else 1, c.lower()))
    else:
        cats.sort(key=lambda c: c.lower())
    return cats

async def fetch_smm_services():
    """Fetch and cache all services from the SMM panel, grouped by category."""
    global _smm_all_services, _smm_categorized, _smm_cache_time
    if not SMM_API_URL or not SMM_API_KEY:
        return False
    if time.time() - _smm_cache_time < 3600 and _smm_categorized:
        return True
    try:
        async with aiohttp.ClientSession() as s:
            async with s.post(SMM_API_URL, data={
                "key": SMM_API_KEY, "action": "services"
            }) as r:
                data = await r.json(content_type=None)
        if not isinstance(data, list):
            return False
        _smm_all_services = data
        _smm_categorized = {}
        for svc in data:
            cat = svc.get("category", "Other") or "Other"
            _smm_categorized.setdefault(cat, []).append(svc)
        _smm_cache_time = time.time()
        logging.info(f"SMM: loaded {len(data)} services across {len(_smm_categorized)} categories")
        return True
    except Exception as e:
        logging.error(f"fetch_smm_services error: {e}")
        return False

async def get_usd_inr() -> float:
    global _usd_inr, _usd_fetched
    if time.time() - _usd_fetched < 3600:
        return _usd_inr
    try:
        async with aiohttp.ClientSession() as s:
            async with s.get("https://open.er-api.com/v6/latest/USD",
                              timeout=aiohttp.ClientTimeout(total=5)) as r:
                d = await r.json()
                if d.get("result") == "success":
                    _usd_inr = float(d["rates"]["INR"])
                    _usd_fetched = time.time()
    except Exception as e:
        logging.warning(f"USD/INR fetch failed, using {_usd_inr}: {e}")
    return _usd_inr

async def build_smm_platform_menu():
    buttons = []
    for p in SMM_PLATFORMS:
        cats = get_smm_platform_categories(p)
        count = sum(len(_smm_categorized[c]) for c in cats)
        emoji = _SMM_PLATFORM_EMOJI.get(p, "🌐")
        label = f"{emoji} {p} ({count} services)"
        buttons.append([Button.inline(label, f"smm_cat_{p}_0".encode(), style="primary")])
    buttons.append([Button.inline("🔍 Search Service", b"smm_search", style="primary")])
    buttons.append([Button.inline("🛒 Place Order (enter Service ID)", b"smm_place_order", style="success")])
    buttons.append([Button.inline("📦 My SMM Orders", b"smm_myorders", style="primary")])
    buttons.append([Button.inline("🔙 Back", b"main", style="primary")])
    text = "🚀 **SMM Services** — choose a platform:"
    return text, buttons

async def build_smm_category_page(platform: str, page: int):
    cat_keys = get_smm_platform_categories(platform)
    total = len(cat_keys)
    tp = max(1, (total + SMM_CATS_PER_PAGE - 1) // SMM_CATS_PER_PAGE)
    page = max(0, min(page, tp - 1))
    chunk = cat_keys[page * SMM_CATS_PER_PAGE:(page + 1) * SMM_CATS_PER_PAGE]

    buttons = []
    for cat in chunk:
        idx = cat_keys.index(cat)
        count = len(_smm_categorized[cat])
        label = f"{cat[:55]} ({count})"
        buttons.append([Button.inline(label, f"smm_svc_{platform}_{idx}_0".encode(), style="primary")])

    nav = []
    if page > 0:
        nav.append(Button.inline("⬅️ Prev", f"smm_cat_{platform}_{page-1}".encode(), style="primary"))
    if page < tp - 1:
        nav.append(Button.inline("Next ➡️", f"smm_cat_{platform}_{page+1}".encode(), style="primary"))
    if nav:
        buttons.append(nav)

    buttons.append([Button.inline("🔍 Search Service", b"smm_search", style="primary")])
    buttons.append([Button.inline("🛒 Place Order (enter Service ID)", b"smm_place_order", style="success")])
    buttons.append([Button.inline("🔙 Back to Platforms", b"smm_services", style="primary")])

    emoji = _SMM_PLATFORM_EMOJI.get(platform, "🌐")
    text = f"{emoji} **{platform} Services** — Select a category  ({page+1}/{tp})\nTotal categories: {total}"
    return text, buttons

async def build_smm_service_page(platform: str, cidx: int, page: int, usd: float):
    cat_keys = get_smm_platform_categories(platform)
    if cidx >= len(cat_keys):
        return None, None
    cat_name = cat_keys[cidx]
    svcs = _smm_categorized[cat_name]
    total = len(svcs)
    tp = max(1, (total + SMM_SVCS_PER_PAGE - 1) // SMM_SVCS_PER_PAGE)
    page = max(0, min(page, tp - 1))
    chunk = svcs[page * SMM_SVCS_PER_PAGE:(page + 1) * SMM_SVCS_PER_PAGE]

    markup = await get_smm_markup(cat_name)
    lines = [f"📋 **{cat_name}**  ({page+1}/{tp})\n"]
    for svc in chunk:
        rate = round(float(svc["rate"]) * usd * markup, 4)
        lines.append(
            f"🆔 `{svc['service']}`\n"
            f"📦 {svc['name']}\n"
            f"💰 ₹{rate}/1k | Min: {svc['min']} Max: {svc['max']}\n"
        )
    text = "\n".join(lines)

    buttons = []
    nav = []
    if page > 0:
        nav.append(Button.inline("⬅️ Prev", f"smm_svc_{platform}_{cidx}_{page-1}".encode(), style="primary"))
    if page < tp - 1:
        nav.append(Button.inline("Next ➡️", f"smm_svc_{platform}_{cidx}_{page+1}".encode(), style="primary"))
    if nav:
        buttons.append(nav)

    buttons.append([Button.inline("🔍 Search Service", b"smm_search", style="primary")])
    buttons.append([Button.inline("🛒 Place Order (enter Service ID)", b"smm_place_order", style="success")])
    buttons.append([Button.inline("🔙 Back to Categories", f"smm_cat_{platform}_0".encode(), style="primary")])
    return text, buttons

SMM_SEARCH_LIMIT = 15

async def build_smm_search_results(query: str, usd: float):
    q = query.lower().strip()
    matches = [
        svc for svc in _smm_all_services
        if q in svc.get("name", "").lower() or q in svc.get("category", "").lower()
    ][:SMM_SEARCH_LIMIT]

    if not matches:
        text = f"🔍 No services found matching `{query}`."
        buttons = [[Button.inline("🔙 Back", b"smm_services", style="primary")]]
        return text, buttons

    lines = [f"🔍 **Search results for:** `{query}` ({len(matches)} shown)\n"]
    for svc in matches:
        markup = await get_smm_markup(svc.get("category", ""))
        rate = round(float(svc["rate"]) * usd * markup, 4)
        lines.append(
            f"🆔 `{svc['service']}`\n"
            f"📦 {svc['name']}\n"
            f"🏷 {svc.get('category', 'N/A')}\n"
            f"💰 ₹{rate}/1k | Min: {svc['min']} Max: {svc['max']}\n"
        )
    text = "\n".join(lines)
    buttons = [
        [Button.inline("🛒 Place Order (enter Service ID)", b"smm_place_order", style="success")],
        [Button.inline("🔍 Search Again", b"smm_search", style="primary")],
        [Button.inline("🔙 Back", b"smm_services", style="primary")],
    ]
    return text, buttons

# ---------- FORCE JOIN ----------
def parse_chat_id(raw_id: str):
    raw = raw_id.strip()
    if raw.startswith('@'):
        return raw
    try:
        return int(raw)
    except ValueError:
        logging.error(f"Invalid chat ID format: {raw}")
        return None

async def is_user_member_of(chat_id_raw: str, user_id: int) -> bool:
    parsed = parse_chat_id(chat_id_raw)
    if parsed is None:
        return False
    try:
        entity = await bot.get_entity(parsed)
        await bot.get_permissions(entity, user_id)
        return True
    except UserNotParticipantError:
        return False
    except (ChatAdminRequiredError, ChannelPrivateError) as e:
        logging.error(f"Cannot verify membership for '{chat_id_raw}': {e}")
        return False
    except Exception as e:
        logging.error(f"Error checking '{chat_id_raw}': {type(e).__name__}: {e}")
        return False

async def is_user_member(user_id: int) -> bool:
    if not RAW_CHAT_IDS:
        return True
    for raw_id in RAW_CHAT_IDS:
        if not await is_user_member_of(raw_id, user_id):
            return False
    return True

async def send_join_message(event):
    is_callback = isinstance(event, events.CallbackQuery.Event)
    buttons = []
    for raw_id in RAW_CHAT_IDS:
        if await is_user_member_of(raw_id, event.sender_id):
            continue
        title = raw_id
        try:
            parsed = parse_chat_id(raw_id)
            entity = await bot.get_entity(parsed)
            title = getattr(entity, 'title', raw_id)
        except Exception as e:
            logging.warning(f"Could not get title for {raw_id}: {e}")
        if raw_id.startswith('@'):
            link = f"https://t.me/{raw_id[1:]}"
            buttons.append([Button.url(f"📢 Join {title}", link, style="primary")])
        else:
            invite_link = None
            try:
                result = await bot(functions.messages.ExportChatInviteRequest(
                    peer=entity,
                    expire_date=None,
                    usage_limit=0
                ))
                invite_link = result.link
            except ChatAdminRequiredError:
                logging.error(f"Bot is not admin in '{raw_id}', cannot generate invite link.")
            except Exception as e:
                logging.error(f"Failed to export invite for '{raw_id}': {type(e).__name__}: {e}")
            if invite_link:
                buttons.append([Button.url(f"📢 Join {title}", invite_link, style="primary")])
            else:
                buttons.append([Button.inline(f"🔒 {title} (join manually)", b"noop", style="primary")])
    if not buttons:
        return
    buttons.append([Button.inline("✅ Check Again", b"check_join", style="success")])
    msg = "🔒 **You must join the channels below to use the bot.**"
    if is_callback:
        await event.edit(msg, buttons=buttons)
    else:
        await event.respond(msg, buttons=buttons)

# ---------- WELCOME / MAIN MENU ----------
async def show_welcome_menu(event, user_id):
    username = await get_bot_username()
    ref_link = f"https://t.me/{username}?start=ref{user_id}" if username else "N/A"
    welcome_msg = (
        "👋 **Welcome to the OTP Shop Bot!**\n\n"
        "🔐 **Buy Telegram Accounts** – Get login OTP & 2FA password instantly.\n"
        "💳 **Deposit via UPI/QR** – Send payment screenshot for approval.\n"
        "🌍 **Multiple Countries & Prices** – Choose country, see price‑wise stock.\n\n"
        "Use the buttons below to get started."
    )
    buttons = [
        [Button.inline("🛒 Buy Account", b"buy", style="success"), Button.inline("💰 My Balance", b"balance", style="primary")],
        [Button.inline("💳 Deposit", b"deposit", style="primary"), Button.inline("📜 Order History", b"orders", style="primary")],
        [Button.inline("🚀 SMM Services", b"smm_services", style="success")],
    ]
    row3 = [Button.inline("👥 Referral Program", b"referral_info", style="primary")]
    if user_id in ADMIN_IDS:
        row3.append(Button.inline("⚙️ Admin Panel", b"admin", style="primary"))
    buttons.append(row3)

    support_link = await get_support_link()
    if support_link:
        buttons.append([Button.url("📞 Support", support_link, style="primary")])

    if isinstance(event, events.CallbackQuery.Event):
        await event.edit(welcome_msg, buttons=buttons)
    else:
        await event.respond(welcome_msg, buttons=buttons)

# ---------- MAIN MENU ----------
async def send_main_menu(event):
    user_id = event.sender_id
    if not await is_user_member(user_id):
        await send_join_message(event)
        return
    buttons = [
        [Button.inline("🛒 Buy Account", b"buy", style="success"), Button.inline("💰 My Balance", b"balance", style="primary")],
        [Button.inline("💳 Deposit", b"deposit", style="primary"), Button.inline("📜 Order History", b"orders", style="primary")],
        [Button.inline("🚀 SMM Services", b"smm_services", style="success")],
    ]
    row3 = [Button.inline("👥 Referral Program", b"referral_info", style="primary")]
    if user_id in ADMIN_IDS:
        row3.append(Button.inline("⚙️ Admin Panel", b"admin", style="primary"))
    buttons.append(row3)
    support_link = await get_support_link()
    if support_link:
        buttons.append([Button.url("📞 Support", support_link, style="primary")])
    msg = "🌟 **OTP Bot Main Menu**"
    if isinstance(event, events.CallbackQuery.Event):
        await event.edit(msg, buttons=buttons)
    else:
        await event.respond(msg, buttons=buttons)

# ---------- BROADCAST COMMAND ----------
@bot.on(events.NewMessage(pattern=r'^/broadcast(?:$|\s)'))
async def broadcast_cmd(event):
    user_id = event.sender_id
    if user_id not in ADMIN_IDS:
        await event.respond("❌ Unauthorized.")
        return

    args = event.message.text.split()
    is_forward = "--f" in args
    pin_dm = "--p" in args
    pin_logs = "--pin" in args

    msg_parts = [arg for arg in args if not arg.startswith("--")]
    if msg_parts and msg_parts[0] == "/broadcast":
        msg_parts = msg_parts[1:]
    msg_text = " ".join(msg_parts).strip()

    has_reply = event.message.is_reply
    replied = None
    if has_reply:
        replied = await event.message.get_reply_message()
        if not replied:
            await event.respond("❌ Could not get replied message.")
            return

    if is_forward:
        if not has_reply:
            await event.respond("❌ Please reply to a message to forward with --f.")
            return
        forward_mode = True
    else:
        if has_reply and not msg_text:
            if replied.text:
                msg_text = replied.text
                forward_mode = False
            else:
                await event.respond(
                    "❌ Replied message has no text to copy. Use `--f` to forward media.",
                    parse_mode='markdown'
                )
                return
        else:
            forward_mode = False
            if not msg_text:
                await event.respond(
                    "❌ Please provide a message to broadcast, or reply to a message to copy its text."
                )
                return

    cursor = users_col.find({}, {"user_id": 1})
    users = await cursor.to_list(length=None)
    user_ids = [u["user_id"] for u in users]
    if not user_ids:
        await event.respond("❌ No users found.")
        return

    user_states[user_id] = {
        "action": "broadcast_confirm",
        "is_forward": forward_mode,
        "pin_dm": pin_dm,
        "pin_logs": pin_logs,
        "msg_text": msg_text if not forward_mode else None,
        "replied_msg": replied if forward_mode else None,
        "user_ids": user_ids,
        "total": len(user_ids),
    }

    preview = "📢 **Broadcast Preview**\n\n"
    preview += f"👥 **Recipients:** {len(user_ids)} users\n"
    if forward_mode:
        preview += "🔄 **Mode:** Forward (replied message will be forwarded)\n"
        if replied and replied.text:
            preview += f"📝 **Preview of replied message:**\n`{replied.text[:200]}`\n"
        if replied and replied.media:
            preview += "📎 *Media will be forwarded.*\n"
    else:
        preview += "📝 **Mode:** Copy text (sender name not included)\n"
        preview += f"📝 **Message:**\n`{msg_text[:500]}`\n"
    if pin_dm:
        preview += "📌 **DM Pin:** Yes (pin in each user's private chat)\n"
    if pin_logs and LOGS_CHANNEL_ID:
        preview += "📌 **Logs Pin:** Yes (pin in logs channel)\n"
    preview += "\nDo you want to send this broadcast?"

    buttons = [
        [Button.inline("✅ Confirm", b"broadcast_confirm", style="success")],
        [Button.inline("❌ Cancel", b"broadcast_cancel", style="danger")],
    ]
    await event.respond(preview, buttons=buttons)

@bot.on(events.CallbackQuery(pattern=b"^broadcast_(confirm|cancel)$"))
async def broadcast_callback(event):
    user_id = event.sender_id
    if user_id not in ADMIN_IDS:
        await event.answer("❌ Unauthorized.", alert=True)
        return

    data = event.data.decode()

    if data == "broadcast_cancel":
        state = user_states.pop(user_id, None)
        if state and state.get("action") == "broadcast_confirm":
            await event.edit("❌ Broadcast cancelled.")
            await event.answer("Cancelled", alert=True)
        else:
            await event.answer("No broadcast to cancel.", alert=True)
        return

    state = user_states.get(user_id)
    if not state or state.get("action") != "broadcast_confirm":
        await event.answer("No pending broadcast.", alert=True)
        return

    is_forward = state["is_forward"]
    pin_dm = state["pin_dm"]
    pin_logs = state["pin_logs"]
    msg_text = state["msg_text"]
    replied = state["replied_msg"]
    user_ids = state["user_ids"]
    total = len(user_ids)

    await event.edit("⏳ **Sending broadcast...** (this may take a while)")
    await event.answer("Broadcast started!", alert=True)

    if pin_logs and LOGS_CHANNEL_ID:
        try:
            if is_forward:
                pin_msg = await bot.forward_messages(LOGS_CHANNEL_ID, replied)
            else:
                pin_msg = await bot.send_message(LOGS_CHANNEL_ID, msg_text, parse_mode="markdown")
            await bot.pin_message(LOGS_CHANNEL_ID, pin_msg, notify=False)
            admin_bc_name = await get_display_name(user_id)
            await log_event(
                f"📌 **Broadcast Pinned**\n"
                f"👤 Admin: {admin_bc_name} (`{user_id}`)\n"
                f"🕐 Time: {now_ist().strftime('%d/%m/%Y %H:%M:%S')} IST"
            )
        except Exception as e:
            logging.error(f"Failed to pin broadcast in logs channel: {e}")
            await event.respond(f"⚠️ Could not pin in logs channel: {e}")

    batch_size = 30
    sent_count = 0
    pin_success = 0
    pin_failed = 0

    for i in range(0, total, batch_size):
        batch = user_ids[i:i + batch_size]

        async def send_one(uid):
            if is_forward:
                return await bot.forward_messages(uid, replied)
            return await bot.send_message(uid, msg_text, parse_mode="markdown")

        results = await asyncio.gather(*(send_one(uid) for uid in batch), return_exceptions=True)

        for uid, res in zip(batch, results):
            if isinstance(res, Exception):
                logging.warning(f"Send failed for {uid}: {res}")
                continue

            sent_count += 1

            if pin_dm:
                try:
                    await bot.pin_message(uid, res, notify=False)
                    pin_success += 1
                except Exception as e:
                    pin_failed += 1
                    logging.warning(f"DM pin failed for {uid}: {e}")

        await asyncio.sleep(1)

    final = "✅ **Broadcast completed!**\n"
    final += f"📤 Sent to {sent_count} out of {total} users.\n"
    if pin_dm:
        final += f"📌 DM pins: {pin_success} success, {pin_failed} failed.\n"
    if pin_logs and LOGS_CHANNEL_ID:
        final += "📌 Logs pin: Done (if successful).\n"

    await event.edit(final)
    user_states.pop(user_id, None)

# ============================================================
#  1. ADMIN LIST FUNCTIONS (Accounts, Transactions, Withdrawals)
# ============================================================

# ---------- Accounts: status filter + pagination ----------
PAGE_SIZE = 20

async def show_all_accounts(event, user_id, status_filter="all", page=0):
    """Show accounts, filtered by status (available/sold/invalid/all), paginated."""
    try:
        query = {}
        if status_filter and status_filter != "all":
            query["status"] = status_filter

        total_count = await accounts_col.count_documents(query)
        total_pages = max(1, (total_count + PAGE_SIZE - 1) // PAGE_SIZE)

        # clamp page within valid range
        page = max(0, min(page, total_pages - 1))
        skip = page * PAGE_SIZE

        cursor = accounts_col.find(query).sort("_id", -1).skip(skip).limit(PAGE_SIZE)
        accounts = await cursor.to_list(length=PAGE_SIZE)

        status_emoji = {
            "available": "🟢",
            "sold": "🔴",
            "invalid": "⚫",
            "inactive": "⚪",
        }

        label = status_filter.capitalize() if status_filter != "all" else "All"

        if not accounts:
            txt = f"📋 **Accounts - {label}**\n\nNo accounts found."
        else:
            lines = []
            for acc in accounts:
                emoji = status_emoji.get(acc.get("status"), "❓")
                buyer = f" (buyer:{acc['buyer_id']})" if acc.get("buyer_id") else ""
                lines.append(
                    f"{emoji} `{acc['phone']}` | {acc['country']} | ₹{acc.get('price', '?')} | {acc.get('status', 'unknown')}{buyer}"
                )
            txt = (
                f"📋 **Accounts - {label}** (Total: {total_count} | Page {page+1}/{total_pages})\n"
                + "\n".join(lines)
            )

        # ---- Filter buttons (row 1 & 2) ----
        def filter_btn(text, status):
            # highlight current filter
            display = f"• {text} •" if status == status_filter else text
            return Button.inline(display, f"admin_accounts|{status}|0".encode(), style="primary")

        filter_row1 = [filter_btn("🟢 Available", "available"), filter_btn("🔴 Sold", "sold")]
        filter_row2 = [filter_btn("⚫ Invalid", "invalid"), filter_btn("📋 All", "all")]

        # ---- Pagination buttons (row 3) ----
        nav_row = []
        if page > 0:
            nav_row.append(Button.inline("⬅️ Prev", f"admin_accounts|{status_filter}|{page-1}".encode(), style="primary"))
        if total_pages > 1:
            nav_row.append(Button.inline(f"{page+1}/{total_pages}", b"noop", style="primary"))
        if page < total_pages - 1:
            nav_row.append(Button.inline("Next ➡️", f"admin_accounts|{status_filter}|{page+1}".encode(), style="primary"))

        buttons = [filter_row1, filter_row2]
        if nav_row:
            buttons.append(nav_row)
        buttons.append([Button.inline("🔙 Back", b"admin", style="primary")])

        await event.edit(txt, buttons=buttons)
    except Exception as e:
        logging.error(f"Error in show_all_accounts: {e}", exc_info=True)
        await event.edit("❌ Error loading accounts. Please try again.", buttons=[[Button.inline("🔙 Back", b"admin", style="primary")]])


# ---------- Transactions: type filter + pagination ----------
TXN_PAGE_SIZE = 20

async def show_all_transactions(event, user_id, type_filter="all", page=0):
    """Show transactions, filtered by type (purchase/deposit/all), paginated."""
    try:
        combined = []
        total_count = 0
        total_pages = 1

        if type_filter == "purchase":
            total_count = await orders_col.count_documents({})
            total_pages = max(1, (total_count + TXN_PAGE_SIZE - 1) // TXN_PAGE_SIZE)
            page = max(0, min(page, total_pages - 1))
            skip = page * TXN_PAGE_SIZE

            cursor = orders_col.find({}).sort("created_at", -1).skip(skip).limit(TXN_PAGE_SIZE)
            orders = await cursor.to_list(length=TXN_PAGE_SIZE)
            for o in orders:
                combined.append({
                    "type": "Purchase",
                    "user_id": o["user_id"],
                    "phone": o.get("phone", "N/A"),
                    "country": o.get("country", "N/A"),
                    "amount": o.get("amount", 0),
                    "date": o["created_at"],
                })

        elif type_filter == "deposit":
            query = {"status": "approved"}
            total_count = await deposits_col.count_documents(query)
            total_pages = max(1, (total_count + TXN_PAGE_SIZE - 1) // TXN_PAGE_SIZE)
            page = max(0, min(page, total_pages - 1))
            skip = page * TXN_PAGE_SIZE

            cursor = deposits_col.find(query).sort("created_at", -1).skip(skip).limit(TXN_PAGE_SIZE)
            deposits = await cursor.to_list(length=TXN_PAGE_SIZE)
            for d in deposits:
                combined.append({
                    "type": "Deposit",
                    "user_id": d["user_id"],
                    "phone": "N/A",
                    "country": "N/A",
                    "amount": d.get("amount", 0),
                    "date": d["created_at"],
                    "txn_id": d.get("txn_id", "N/A"),
                })

        else:  # "all" -> merge both collections, in-memory sort + paginate
            orders_cursor = orders_col.find({}).sort("created_at", -1)
            deposits_cursor = deposits_col.find({"status": "approved"}).sort("created_at", -1)
            orders = await orders_cursor.to_list(length=None)
            deposits = await deposits_cursor.to_list(length=None)

            all_items = []
            for o in orders:
                all_items.append({
                    "type": "Purchase",
                    "user_id": o["user_id"],
                    "phone": o.get("phone", "N/A"),
                    "country": o.get("country", "N/A"),
                    "amount": o.get("amount", 0),
                    "date": o["created_at"],
                })
            for d in deposits:
                all_items.append({
                    "type": "Deposit",
                    "user_id": d["user_id"],
                    "phone": "N/A",
                    "country": "N/A",
                    "amount": d.get("amount", 0),
                    "date": d["created_at"],
                    "txn_id": d.get("txn_id", "N/A"),
                })

            all_items.sort(key=lambda x: x["date"], reverse=True)
            total_count = len(all_items)
            total_pages = max(1, (total_count + TXN_PAGE_SIZE - 1) // TXN_PAGE_SIZE)
            page = max(0, min(page, total_pages - 1))
            skip = page * TXN_PAGE_SIZE

            combined = all_items[skip: skip + TXN_PAGE_SIZE]

        logging.info(f"[DEBUG] type_filter={type_filter} page={page} total_count={total_count} total_pages={total_pages}")

        label = type_filter.capitalize() if type_filter != "all" else "All"

        if not combined:
            txt = f"📜 **Transaction History - {label}**\n\nNo transactions found."
        else:
            lines = []
            for item in combined:
                date_str = item["date"].strftime('%d/%m/%Y %H:%M')
                if item["type"] == "Purchase":
                    lines.append(
                        f"🛒 User {item['user_id']} | {item['phone']} ({item['country']}) | -₹{item['amount']} | {date_str}"
                    )
                else:
                    lines.append(
                        f"💰 User {item['user_id']} | Deposit | +₹{item['amount']} | Txn:{item.get('txn_id','N/A')} | {date_str}"
                    )
            txt = (
                f"📜 **Transaction History - {label}** (Total: {total_count} | Page {page+1}/{total_pages})\n"
                + "\n".join(lines)
            )

        # ---- Filter buttons ----
        def filter_btn(text, t_filter):
            display = f"• {text} •" if t_filter == type_filter else text
            return Button.inline(display, f"admin_transactions|{t_filter}|0".encode(), style="primary")

        filter_row = [
            filter_btn("🛒 Purchase", "purchase"),
            filter_btn("💰 Deposit", "deposit"),
            filter_btn("📋 All", "all"),
        ]

        # ---- Pagination buttons ----
        nav_row = []
        if page > 0:
            nav_row.append(Button.inline("⬅️ Prev", f"admin_transactions|{type_filter}|{page-1}".encode(), style="primary"))
        if total_pages > 1:
            nav_row.append(Button.inline(f"{page+1}/{total_pages}", b"noop", style="primary"))
        if page < total_pages - 1:
            nav_row.append(Button.inline("Next ➡️", f"admin_transactions|{type_filter}|{page+1}".encode(), style="primary"))

        buttons = [filter_row]
        if nav_row:
            buttons.append(nav_row)
        buttons.append([Button.inline("🔙 Back", b"admin", style="primary")])

        await event.edit(txt, buttons=buttons)
    except Exception as e:
        logging.error(f"Error in show_all_transactions: {e}", exc_info=True)
        await event.edit("❌ Error loading transactions.", buttons=[[Button.inline("🔙 Back", b"admin", style="primary")]])


async def fetch_smm_order_status(order_ids: list) -> dict:
    """Fetch live status for one or more SMM orders from the panel.
    Returns {str(order_id): {"status": ..., "charge": ..., ...}} or {} on failure."""
    if not order_ids or not SMM_API_URL or not SMM_API_KEY:
        return {}
    try:
        async with aiohttp.ClientSession() as s:
            if len(order_ids) == 1:
                async with s.post(SMM_API_URL, data={
                    "key": SMM_API_KEY, "action": "status", "order": str(order_ids[0])
                }) as r:
                    data = await r.json(content_type=None)
                if isinstance(data, dict) and "error" not in data:
                    return {str(order_ids[0]): data}
                return {}
            else:
                async with s.post(SMM_API_URL, data={
                    "key": SMM_API_KEY, "action": "status",
                    "orders": ",".join(str(o) for o in order_ids)
                }) as r:
                    data = await r.json(content_type=None)
                return data if isinstance(data, dict) else {}
    except Exception as e:
        logging.error(f"fetch_smm_order_status error: {e}")
        return {}

SMM_STATUS_EMOJI = {
    "pending": "🟡", "completed": "🟢", "in progress": "🔵",
    "processing": "🔵", "partial": "🟠", "canceled": "🔴", "cancelled": "🔴"
}

# ---------- SMM Orders: paginated list (admin) ----------
SMM_ORDERS_PAGE_SIZE = 15

async def show_all_smm_orders(event, user_id, page=0):
    """Show all SMM orders placed through the bot, paginated, newest first."""
    try:
        total_count = await smm_orders_col.count_documents({})
        total_pages = max(1, (total_count + SMM_ORDERS_PAGE_SIZE - 1) // SMM_ORDERS_PAGE_SIZE)
        page = max(0, min(page, total_pages - 1))
        skip = page * SMM_ORDERS_PAGE_SIZE

        cursor = smm_orders_col.find({}).sort("created_at", -1).skip(skip).limit(SMM_ORDERS_PAGE_SIZE)
        orders = await cursor.to_list(length=SMM_ORDERS_PAGE_SIZE)

        if not orders:
            txt = "🚀 **SMM Order History**\n\nNo SMM orders found."
        else:
            order_ids = [o.get("smm_order_id") for o in orders if o.get("smm_order_id")]
            live_status = await fetch_smm_order_status(order_ids)
            for o in orders:
                sid = str(o.get("smm_order_id", ""))
                live = live_status.get(sid)
                if live and live.get("status"):
                    o["status"] = live["status"]
                    try:
                        await smm_orders_col.update_one(
                            {"_id": o["_id"]}, {"$set": {"status": live["status"]}}
                        )
                    except Exception:
                        pass

            lines = []
            for o in orders:
                date_str = o["created_at"].strftime('%d/%m/%Y %H:%M')
                status_text = o.get("status", "pending")
                st = SMM_STATUS_EMOJI.get(str(status_text).lower(), "❓")
                lines.append(
                    f"{st} User `{o['user_id']}` | {o.get('service_name', '?')[:28]}\n"
                    f"   Status: **{status_text}** | Qty: {o.get('quantity', 0)} | ₹{o.get('charge', 0)}\n"
                    f"   SMM ID: `{o.get('smm_order_id', 'N/A')}` | {date_str}"
                )
            txt = (
                f"🚀 **SMM Order History** (Total: {total_count} | Page {page+1}/{total_pages})\n\n"
                + "\n\n".join(lines)
            )

        nav_row = []
        if page > 0:
            nav_row.append(Button.inline("⬅️ Prev", f"admin_smm_orders|{page-1}".encode(), style="primary"))
        if total_pages > 1:
            nav_row.append(Button.inline(f"{page+1}/{total_pages}", b"noop", style="primary"))
        if page < total_pages - 1:
            nav_row.append(Button.inline("Next ➡️", f"admin_smm_orders|{page+1}".encode(), style="primary"))

        buttons = []
        if nav_row:
            buttons.append(nav_row)
        buttons.append([Button.inline("🔙 Back", b"admin", style="primary")])

        await event.edit(txt, buttons=buttons)
    except Exception as e:
        logging.error(f"Error in show_all_smm_orders: {e}", exc_info=True)
        await event.edit("❌ Error loading SMM orders.", buttons=[[Button.inline("🔙 Back", b"admin", style="primary")]])


# ---------- Withdrawals: simple list (latest 50) ----------
async def show_all_withdrawals(event, user_id):
    """Show all withdrawal requests (latest 50)."""
    try:
        cursor = withdrawals_col.find({}).sort("created_at", -1)
        withdrawals = await cursor.to_list(length=None)
        if not withdrawals:
            txt = "💸 **Withdrawal History**\n\nNo withdrawals found."
        else:
            lines = []
            for wd in withdrawals[:50]:
                status_emoji = {"pending": "🟡", "approved": "🟢", "rejected": "🔴"}.get(wd.get("status"), "❓")
                date_str = wd["created_at"].strftime('%d/%m/%Y %H:%M')
                lines.append(
                    f"{status_emoji} User {wd['user_id']} | ₹{wd['amount']} | UPI: {wd.get('upi_id','N/A')} | {wd.get('status','unknown')} | {date_str}"
                )
            txt = f"💸 **Withdrawal History** (Latest {len(lines)})\n" + "\n".join(lines)

        await event.edit(txt, buttons=[[Button.inline("🔙 Back", b"admin", style="primary")]])
    except Exception as e:
        logging.error(f"Error in show_all_withdrawals: {e}", exc_info=True)
        await event.edit("❌ Error loading withdrawals.", buttons=[[Button.inline("🔙 Back", b"admin", style="primary")]])


# ============================================================
#  2. CALLBACK HANDLER
# ============================================================

@bot.on(events.CallbackQuery)
async def callback_handler(event):
    try:
        data = event.data.decode("utf-8")
        user_id = event.sender_id
        logging.info(f"Callback received: {data} from user {user_id}")

        # Skip broadcast callbacks (handled separately)
        if data.startswith("broadcast_"):
            return

        # ---------- CHECK JOIN ----------
        if data == "check_join":
            if await is_user_member(user_id):
                await show_welcome_menu(event, user_id)
            else:
                await event.answer("You haven't joined all channels yet!", alert=True)
                await send_join_message(event)
            return

        if not await is_user_member(user_id):
            await event.answer("You must join all channels first!", alert=True)
            await send_join_message(event)
            return

        # Clear states for menu switches
        if data in ("main", "buy", "balance", "deposit", "orders", "admin",
                    "admin_add_otp", "admin_add_sess", "admin_add_stock",
                    "admin_addbal", "admin_deposits",
                    "admin_setprice", "admin_support", "withdraw", "admin_minwithdraw",
                    "admin_transactions", "admin_withdrawals", "my_withdrawals",
                    "smm_services", "smm_myorders", "admin_smm_markup", "smm_search",
                    "admin_smm_markup_default", "admin_smm_markup_member", "admin_smm_orders",
                    "admin_cat_accounts", "admin_cat_finance", "admin_cat_smm", "admin_cat_settings",
                    "admin_referral_settings", "admin_set_ref_percent", "admin_set_ref_max"):
            user_states.pop(user_id, None)

        # ---------- ADMIN ACCOUNTS (filter + pagination) ----------
        if data.startswith("admin_accounts"):
            if user_id not in ADMIN_IDS:
                await event.answer("❌ Unauthorized", alert=True)
                return

            if data == "admin_accounts":
                status_filter, page = "all", 0
            else:
                try:
                    _, status_filter, page_str = data.split("|")
                    page = int(page_str)
                except (ValueError, IndexError):
                    status_filter, page = "all", 0

            await show_all_accounts(event, user_id, status_filter, page)
            await event.answer()
            return

        # ---------- ADMIN TRANSACTIONS (type filter + pagination) ----------
        if data.startswith("admin_transactions"):
            if user_id not in ADMIN_IDS:
                await event.answer("❌ Unauthorized", alert=True)
                return

            if data == "admin_transactions":
                type_filter, page = "all", 0
            else:
                try:
                    _, type_filter, page_str = data.split("|")
                    page = int(page_str)
                except (ValueError, IndexError):
                    type_filter, page = "all", 0

            await show_all_transactions(event, user_id, type_filter, page)
            await event.answer()
            return

        # ---------- ADMIN SMM ORDERS (record/logs view) ----------
        if data.startswith("admin_smm_orders"):
            if user_id not in ADMIN_IDS:
                await event.answer("❌ Unauthorized", alert=True)
                return
            if data == "admin_smm_orders":
                page = 0
            else:
                try:
                    _, page_str = data.split("|")
                    page = int(page_str)
                except (ValueError, IndexError):
                    page = 0
            await show_all_smm_orders(event, user_id, page)
            await event.answer()
            return

        # ---------- ADMIN WITHDRAWALS (simple) ----------
        if data == "admin_withdrawals":
            if user_id not in ADMIN_IDS:
                await event.answer("❌ Unauthorized", alert=True)
                return
            await show_all_withdrawals(event, user_id)
            await event.answer()
            return

        # ---------- NOOP (page indicator, does nothing) ----------
        if data == "noop":
            await event.answer()
            return

        # ---------- USER WITHDRAWALS PAGINATION ----------
        if data.startswith("user_wd_page_"):
            try:
                page = int(data.split("_")[3])
                if page < 1:
                    page = 1
                user_states[user_id] = {"user_wd_page": page}
            except:
                await event.answer("Invalid page", alert=True)
                return
            await show_user_withdrawals(event, user_id)
            await event.answer()
            return

        # ---------- MANAGE SESSIONS ----------
        async def render_sessions(phone):
            auths = await acc_mgr.get_authorizations(phone)
            if auths is None:
                return (f"❌ Could not fetch sessions for `{phone}`. "
                        f"The account's monitoring client may be offline.", None)
            if not auths:
                return f"📱 No active sessions found for `{phone}`.", [
                    [Button.inline("🔙 Close", b"close_sessions", style="primary")]
                ]

            lines = [f"📱 **Active Sessions — ** `{phone}`\n"]
            buttons = []
            for a in auths:
                date_str = a.date_active.strftime('%d/%m/%Y %H:%M') if a.date_active else "N/A"
                tag = " ⭐ *(bot's session — used for OTP delivery)*" if a.current else ""
                lines.append(
                    f"{'⭐' if a.current else '📟'} **{a.device_model or 'Unknown device'}**{tag}\n"
                    f"   App: {a.app_name} {a.app_version}\n"
                    f"   Platform: {a.platform} {a.system_version}\n"
                    f"   Last active: {date_str}\n"
                    f"   IP: {a.ip} ({a.country})\n"
                )
                device_label = (a.device_model or 'device')[:20]
                if a.current:
                    buttons.append([Button.inline(
                        f"⚠️ Terminate Bot's Session",
                        f"termcurr_{phone}_{a.hash}", style="danger"
                    )])
                else:
                    buttons.append([Button.inline(
                        f"❌ Terminate: {device_label}",
                        f"termsess_{phone}_{a.hash}", style="danger"
                    )])

            buttons.append([Button.inline("🔄 Refresh", f"sessions_{phone}", style="primary")])
            buttons.append([Button.inline("🔙 Close", b"close_sessions", style="primary")])
            return "\n".join(lines), buttons

        if data.startswith("sessions_"):
            phone = data[len("sessions_"):]
            text, buttons = await render_sessions(phone)
            try:
                await event.edit(text, buttons=buttons)
            except MessageNotModifiedError:
                pass
            await event.answer()
            return

        # Terminating the BOT'S OWN session requires an explicit warning + confirmation
        if data.startswith("termcurr_"):
            _, phone, hash_str = data.split("_", 2)
            await event.answer(
                "⚠️ This is the bot's own OTP session! Terminating it stops "
                "further OTPs and disables session management here.",
                alert=True
            )
            confirm_buttons = [
                [Button.inline("⚠️ Yes, Terminate Anyway", f"termcurrok_{phone}_{hash_str}", style="danger")],
                [Button.inline("❌ Cancel", f"sessions_{phone}", style="primary")],
            ]
            new_text = (
                f"⚠️ **Are you sure?**\n\n"
                f"This is the **bot's own session** for `{phone}` — the one used to "
                f"forward OTPs to you.\n\n"
                f"❌ Terminating it means:\n"
                f"• You will **not receive any further OTPs** on this number.\n"
                f"• You will **not be able to manage sessions** here anymore.\n\n"
                f"This action cannot be undone from this bot. Proceed?"
            )
            try:
                await event.edit(new_text, buttons=confirm_buttons)
            except MessageNotModifiedError:
                pass
            return

        if data.startswith("termcurrok_"):
            _, phone, hash_str = data.split("_", 2)
            ok, msg = await acc_mgr.terminate_own_session(phone)
            if ok:
                await event.answer("✅ Bot's session terminated. OTP delivery stopped.", alert=True)
                try:
                    await event.edit(
                        f"🔒 **Bot's session for** `{phone}` **has been terminated.**\n\n"
                        f"You will no longer receive OTPs on this number through this bot, "
                        f"and session management is no longer available here.",
                        buttons=None
                    )
                except MessageNotModifiedError:
                    pass
            else:
                await event.answer((f"❌ {msg}")[:200], alert=True)
            return

        if data.startswith("termsess_"):
            _, phone, hash_str = data.split("_", 2)
            confirm_buttons = [
                [Button.inline("✅ Yes, Terminate", f"termsessok_{phone}_{hash_str}", style="danger")],
                [Button.inline("❌ Cancel", f"sessions_{phone}", style="primary")],
            ]
            new_text = (
                f"⚠️ **Terminate this session?**\n\n"
                f"This device will be logged out of `{phone}` immediately.\n"
                f"This action cannot be undone. Proceed?"
            )
            try:
                await event.edit(new_text, buttons=confirm_buttons)
            except MessageNotModifiedError:
                pass
            await event.answer()
            return

        if data.startswith("termsessok_"):
            _, phone, hash_str = data.split("_", 2)
            try:
                hash_id = int(hash_str)
            except ValueError:
                await event.answer("❌ Invalid session.", alert=True)
                return
            ok, msg = await acc_mgr.terminate_session(phone, hash_id)
            await event.answer((("✅ " if ok else "❌ ") + msg)[:200], alert=True)
            text, buttons = await render_sessions(phone)
            try:
                await event.edit(text, buttons=buttons)
            except MessageNotModifiedError:
                pass
            return

        if data == "close_sessions":
            try:
                await event.delete()
            except Exception:
                pass
            await event.answer()
            return

        # ---------- REFERRAL INFO ----------
        if data == "referral_info":
            username = await get_bot_username()
            ref_link = f"https://t.me/{username}?start=ref{user_id}" if username else "N/A"
            invited_count = await users_col.count_documents({"referred_by": user_id})
            paid_count = await users_col.count_documents({"referred_by": user_id, "referral_bonus_paid": True})
            user_doc = await users_col.find_one({"user_id": user_id})
            withdrawable = user_doc.get('withdrawable_balance', 0) if user_doc else 0
            total_earned = round(user_doc.get('referral_earnings', 0), 2) if user_doc else 0
            cur_percent = await get_referral_bonus_percent()
            cur_max = await get_referral_bonus_max()
            min_wd = await get_min_withdrawal()

            text = (
                "👥 **Referral Program**\n\n"
                "🔗 **Your Referral Link:**\n"
                f"`{ref_link}`\n\n"
                f"💰 **Bonus:** {cur_percent}% of your referral's **first deposit**, "
                f"up to ₹{cur_max} max\n\n"
                "📊 **Your Stats:**\n"
                f"• Total Invited: **{invited_count}** users\n"
                f"• Bonus Paid: **{paid_count}** users\n"
                f"• Total Earned: **₹{total_earned}**\n\n"
                f"💸 **Withdrawable Balance:** ₹{withdrawable}\n"
                f"📏 **Minimum Withdrawal:** ₹{min_wd}\n\n"
                "Share your link and start earning!"
            )
            buttons = [
                [Button.inline("💸 Withdraw", b"withdraw", style="primary")],
                [Button.inline("📜 Withdrawal History", b"my_withdrawals", style="primary")],
                [Button.inline("🔙 Back", b"main", style="primary")]
            ]
            await event.edit(text, buttons=buttons)
            await event.answer()
            return

        # ---------- WITHDRAW ----------
        if data == "withdraw":
            user_doc = await users_col.find_one({"user_id": user_id})
            withdrawable = user_doc.get('withdrawable_balance', 0) if user_doc else 0
            min_wd = await get_min_withdrawal()
            if withdrawable <= 0:
                await event.answer(
                    f"❌ You have no withdrawable balance.\nMinimum withdrawal is ₹{min_wd}.",
                    alert=True
                )
                return
            if withdrawable < min_wd:
                await event.answer(
                    f"❌ Your withdrawable balance (₹{withdrawable}) is below the "
                    f"minimum withdrawal of ₹{min_wd}.",
                    alert=True
                )
                return
            user_states[user_id] = {"action": "withdraw", "step": "amount"}
            await event.edit(
                f"💸 **Withdraw**\n\nYour withdrawable balance: ₹{withdrawable}\n"
                f"Minimum withdrawal: ₹{min_wd}\n"
                "Enter the amount you wish to withdraw (in ₹):",
                buttons=[[Button.inline("🔙 Cancel", b"referral_info", style="danger")]]
            )
            await event.answer()
            return

        # ---------- USER WITHDRAWALS HISTORY ----------
        if data == "my_withdrawals":
            await show_user_withdrawals(event, user_id)
            await event.answer()
            return

        # ---------- BUY / COUNTRY / PRICE ----------
        if data == "buy":
            countries = await accounts_col.distinct("country", {"status": "available"})
            if not countries:
                await event.answer("❌ No accounts available!", alert=True)
                return
            btns = [[Button.inline(c, f"country_{c}", style="primary")] for c in countries]
            btns.append([Button.inline("🔙 Back", b"main", style="primary")])
            await event.edit("🌍 Choose a country:", buttons=btns)
            await event.answer()
            return

        if data.startswith("country_"):
            country = data.split("_", 1)[1]
            total_count = await accounts_col.count_documents({"country": country, "status": "available"})
            if total_count == 0:
                await event.answer("No accounts left.", alert=True)
                return
            pipeline = [
                {"$match": {"country": country, "status": "available"}},
                {"$group": {"_id": "$price", "count": {"$sum": 1}}},
                {"$sort": {"_id": 1}}
            ]
            agg = await accounts_col.aggregate(pipeline).to_list(length=None)
            btns = []
            for item in agg:
                price = item["_id"] if item["_id"] is not None else DEFAULT_PRICE
                count = item["count"]
                btns.append([Button.inline(f"₹{price} ({count} available)", f"price_{country}_{price}", style="primary")])
            btns.append([Button.inline("🔙 Back", b"buy", style="primary")])
            await event.edit(
                f"🌍 Country: {country}\n📦 Total Stock: {total_count}\n💵 Select a price:",
                buttons=btns
            )
            await event.answer()
            return

        if data.startswith("price_"):
            parts = data.split("_", 2)
            country = parts[1]
            price = float(parts[2])
            stock = await accounts_col.count_documents({"country": country, "status": "available", "price": price})
            user_states[user_id] = {"action": "awaiting_confirmation", "country": country, "price": price}
            confirm_text = (
                "👋 Dear customer, after you agree to the terms and click the confirm button, "
                "the number will be reserved for you.\n\n"
                "💰 The amount will only be deducted from your account when you successfully receive the login codes.\n\n"
                "⚠️ Please note that cancellation is not available in this server because OTP Delivery is guaranteed!\n\n"
                f"🌏 Country: {country} 🇮🇳\n"
                f"💰 Price: ₹{price}\n"
                f"📦 Stock: {stock}"
            )
            buttons = [
                [Button.inline("✅ Confirm Purchase", b"confirm_purchase", style="success")],
                [Button.inline("❌ Cancel", b"cancel_purchase", style="danger")]
            ]
            await event.edit(confirm_text, buttons=buttons)
            await event.answer()
            return

        # ---------- CONFIRM PURCHASE (full logic kept) ----------
        if data == "confirm_purchase":
            state = user_states.get(user_id)
            if not state or state.get("action") != "awaiting_confirmation":
                await event.answer("Session expired. Please start again.", alert=True)
                return
            country = state["country"]
            price = state["price"]

            user = await users_col.find_one({"user_id": user_id})
            balance = user["balance"] if user else 0
            if balance < price:
                await event.answer("❌ Insufficient balance!", alert=True)
                return

            cursor = accounts_col.find({"country": country, "status": "available", "price": price})
            accounts = await cursor.to_list(length=None)
            if not accounts:
                await event.answer("❌ No accounts available in this category!", alert=True)
                return

            selected_acc = None
            for acc in accounts:
                updated = await accounts_col.find_one_and_update(
                    {"_id": acc["_id"], "status": "available"},
                    {"$set": {"status": "sold", "buyer_id": user_id, "sold_at": now_ist()}}
                )
                if updated is None:
                    continue

                phone = acc["phone"]
                client = acc_mgr.clients.get(phone)
                if client:
                    try:
                        await client.get_me()
                    except Exception as e:
                        error_msg = str(e)[:150]
                        logging.warning(f"Session invalid for {phone}: {e}")
                        await accounts_col.update_one({"_id": updated["_id"]}, {"$set": {"status": "inactive"}})
                        for admin in ADMIN_IDS:
                            try:
                                await bot.send_message(admin,
                                    f"⚠️ **Inactive Session Detected!**\n"
                                    f"📱 Phone: `{phone}`\n"
                                    f"🌍 Country: {country}\n"
                                    f"💰 Price: ₹{price}\n"
                                    f"❌ Error: `{error_msg}`\n"
                                    f"🔄 Status: Marked as `inactive` in DB."
                                )
                            except:
                                pass
                        continue
                else:
                    await accounts_col.update_one({"_id": updated["_id"]}, {"$set": {"status": "inactive"}})
                    continue

                selected_acc = updated
                break

            if selected_acc is None:
                await event.answer("❌ No active accounts available! Please try later.", alert=True)
                return

            acc = selected_acc
            phone = acc["phone"]
            twofa_password = acc.get("twofa_password")

            old_withdrawable = user.get('withdrawable_balance', 0) if user else 0
            new_withdrawable = max(0, old_withdrawable - price)
            deduct_result = await users_col.update_one(
                {"user_id": user_id, "balance": {"$gte": price}},
                {"$inc": {"balance": -price}, "$set": {"withdrawable_balance": new_withdrawable}}
            )
            if deduct_result.modified_count == 0:
                # Balance was insufficient at the moment of deduction (e.g. spent
                # elsewhere concurrently) — release the reserved account back to stock.
                await accounts_col.update_one(
                    {"_id": acc["_id"]},
                    {"$set": {"status": "available"}, "$unset": {"buyer_id": "", "sold_at": ""}}
                )
                await event.answer("❌ Insufficient balance! Please deposit and try again.", alert=True)
                return

            await orders_col.insert_one({
                "user_id": user_id,
                "account_id": str(acc["_id"]),
                "phone": phone,
                "country": country,
                "amount": price,
                "status": "completed",
                "created_at": now_ist()
            })

            success_text = f"✅ **Purchase successful!**\n📱 Your number: `{phone}`\n"
            if twofa_password:
                success_text += f"🔒 **2FA Password:** `{twofa_password}`\n\n"
            success_text += (
                "Now login to Telegram with this number. OTP will appear here automatically.\n"
                "If you need a new OTP later, click below."
            )
            await event.edit(
                success_text,
                buttons=[
                    [Button.inline("🔄 Request New OTP", f"resend_{phone}", style="primary")],
                    [Button.inline("🔙 Main Menu", b"main", style="primary")]
                ]
            )
            user_states.pop(user_id, None)

            # Admin notification
            try:
                buyer_entity = await bot.get_entity(user_id)
                buyer_name = buyer_entity.first_name or buyer_entity.username or str(user_id)
            except:
                buyer_name = str(user_id)
            updated_user = await users_col.find_one({"user_id": user_id})
            new_balance = updated_user["balance"] if updated_user else 0
            for admin in ADMIN_IDS:
                try:
                    await bot.send_message(admin,
                        f"🛒 **New Purchase**\n"
                        f"Buyer: `{user_id}` - {buyer_name}\n"
                        f"Phone: `{phone}`\n"
                        f"Country: {country}\n"
                        f"Price: ₹{price}\n"
                        f"Balance After: ₹{new_balance}"
                    )
                except:
                    pass
            await log_event(
                f"🛒 **New Account Purchase**\n"
                f"👤 Buyer: {buyer_name} (`{user_id}`)\n"
                f"📱 Phone: `{phone}`\n"
                f"🌍 Country: {country}\n"
                f"💰 Price: ₹{price}\n"
                f"👛 Balance After: ₹{new_balance}\n"
                f"🕐 Time: {now_ist().strftime('%d/%m/%Y %H:%M:%S')} IST"
            )
            await event.answer("✅ Purchase successful!", alert=True)
            return

        # ---------- CANCEL PURCHASE ----------
        if data == "cancel_purchase":
            state = user_states.pop(user_id, None)
            if state and state.get("action") == "awaiting_confirmation":
                country = state["country"]
                total_count = await accounts_col.count_documents({"country": country, "status": "available"})
                if total_count == 0:
                    await event.edit("❌ No accounts left in this country.", buttons=[[Button.inline("🔙 Back", b"buy", style="primary")]])
                    return
                pipeline = [
                    {"$match": {"country": country, "status": "available"}},
                    {"$group": {"_id": "$price", "count": {"$sum": 1}}},
                    {"$sort": {"_id": 1}}
                ]
                agg = await accounts_col.aggregate(pipeline).to_list(length=None)
                btns = []
                for item in agg:
                    price_val = item["_id"] if item["_id"] is not None else DEFAULT_PRICE
                    count = item["count"]
                    btns.append([Button.inline(f"₹{price_val} ({count} available)", f"price_{country}_{price_val}", style="primary")])
                btns.append([Button.inline("🔙 Back", b"buy", style="primary")])
                await event.edit(
                    f"🌍 Country: {country}\n📦 Total Stock: {total_count}\n💵 Select a price:",
                    buttons=btns
                )
            else:
                await event.edit("❌ Cancelled.", buttons=[[Button.inline("🔙 Main Menu", b"main", style="primary")]])
            await event.answer()
            return

        # ---------- RESEND OTP ----------
        if data.startswith("resend_"):
            phone = data.split("_", 1)[1]
            if phone not in acc_mgr.clients:
                await event.answer("❌ Session expired. Contact admin.", alert=True)
                return
            pending_otp_requests[(user_id, phone)] = True
            await event.answer("✅ Waiting for new OTP.", alert=True)
            async def clear_pending():
                await asyncio.sleep(90)
                key = (user_id, phone)
                if key in pending_otp_requests:
                    del pending_otp_requests[key]
                    try:
                        await bot.send_message(user_id, "⏰ No OTP received. Try again.")
                    except:
                        pass
            asyncio.create_task(clear_pending())
            return

        # ---------- BALANCE ----------
        if data == "balance":
            user = await users_col.find_one({"user_id": user_id})
            bal = user["balance"] if user else 0
            await event.edit(f"💰 Your balance: ₹{bal}", buttons=[[Button.inline("🔙 Back", b"main", style="primary")]])
            await event.answer()
            return

        # ---------- DEPOSIT ----------
        if data == "deposit":
            user_states[user_id] = {"action": "deposit", "step": "amount"}
            await event.edit(
                f"💵 Enter amount (min ₹{MIN_DEPOSIT}):",
                buttons=[[Button.inline("🔙 Cancel", b"main", style="danger")]]
            )
            await event.answer()
            return

        # ---------- ORDERS ----------
        if data == "orders":
            orders_cursor = orders_col.find({"user_id": user_id}).sort("created_at", -1)
            orders = await orders_cursor.to_list(length=20)
            deposits_cursor = deposits_col.find({"user_id": user_id, "status": "approved"}).sort("created_at", -1)
            deposits = await deposits_cursor.to_list(length=20)
            combined = []
            for o in orders:
                combined.append({
                    "type": "Purchase",
                    "phone": o.get("phone", "N/A"),
                    "country": o.get("country", "N/A"),
                    "amount": o.get("amount", 0),
                    "date": o["created_at"],
                })
            for d in deposits:
                combined.append({
                    "type": "Deposit",
                    "phone": "N/A",
                    "country": "N/A",
                    "amount": d.get("amount", 0),
                    "date": d["created_at"],
                })
            combined.sort(key=lambda x: x["date"], reverse=True)
            combined = combined[:20]
            if not combined:
                txt = "📜 No transactions yet."
            else:
                lines = []
                for item in combined:
                    date_str = item["date"].strftime('%d/%m/%Y')
                    if item["type"] == "Purchase":
                        lines.append(f"🛒 {item['phone']} ({item['country']}) - ₹{item['amount']} - {date_str}")
                    else:
                        lines.append(f"💰 Deposit +₹{item['amount']} - {date_str}")
                txt = "📜 **Transaction History:**\n" + "\n".join(lines)
            await event.edit(txt, buttons=[[Button.inline("🔙 Back", b"main", style="primary")]])
            await event.answer()
            return

        # ---------- MAIN ----------
        if data == "main":
            await send_main_menu(event)
            await event.answer()
            return

        # ============================================================
        #  SMM SERVICES CALLBACKS
        # ============================================================
        if data == "smm_services":
            if not SMM_API_URL or not SMM_API_KEY:
                await event.answer("❌ SMM panel not configured yet.", alert=True)
                return
            ok = await fetch_smm_services()
            if not ok or not _smm_categorized:
                await event.edit("❌ Could not load SMM services. Try again later.",
                                  buttons=[[Button.inline("🔙 Back", b"main", style="primary")]])
                await event.answer()
                return
            text, btns = await build_smm_platform_menu()
            await event.edit(text, buttons=btns)
            await event.answer()
            return

        if data.startswith("smm_cat_"):
            parts = data.split("_")
            platform, page = parts[2], int(parts[3])
            if not _smm_categorized:
                await fetch_smm_services()
            text, btns = await build_smm_category_page(platform, page)
            await event.edit(text, buttons=btns)
            await event.answer()
            return

        if data.startswith("smm_svc_"):
            parts = data.split("_")
            platform, cidx, page = parts[2], int(parts[3]), int(parts[4])
            text, btns = await build_smm_service_page(platform, cidx, page, await get_usd_inr())
            if text is None:
                await event.answer("Not found.", alert=True)
                return
            await event.edit(text, buttons=btns)
            await event.answer()
            return

        if data == "smm_search":
            if not _smm_all_services:
                await fetch_smm_services()
            user_states[user_id] = {"action": "smm_search", "step": "query"}
            await event.edit(
                "🔍 Send a keyword to search services (e.g. `views`, `members`, `reaction`, `Instagram`):",
                buttons=[[Button.inline("🔙 Cancel", b"smm_services", style="danger")]]
            )
            await event.answer()
            return

        if data == "smm_place_order":
            user_states[user_id] = {"action": "smm_order", "step": "service_id"}
            await event.edit(
                "🆔 Enter the **Service ID** you want to order (shown above each service):",
                buttons=[[Button.inline("🔙 Cancel", b"smm_services", style="danger")]]
            )
            await event.answer()
            return

        if data == "smm_myorders":
            cursor = smm_orders_col.find({"user_id": user_id}).sort("created_at", -1)
            orders = await cursor.to_list(length=20)
            if not orders:
                txt = "📦 No SMM orders yet."
            else:
                order_ids = [o.get("smm_order_id") for o in orders if o.get("smm_order_id")]
                live_status = await fetch_smm_order_status(order_ids)
                for o in orders:
                    sid = str(o.get("smm_order_id", ""))
                    live = live_status.get(sid)
                    if live and live.get("status"):
                        o["status"] = live["status"]
                        try:
                            await smm_orders_col.update_one(
                                {"_id": o["_id"]}, {"$set": {"status": live["status"]}}
                            )
                        except Exception:
                            pass

                lines = []
                for o in orders:
                    date_str = o["created_at"].strftime('%d/%m/%Y')
                    status_text = o.get("status", "pending")
                    st = SMM_STATUS_EMOJI.get(str(status_text).lower(), "❓")
                    lines.append(
                        f"🆔 Order `{o.get('smm_order_id', 'N/A')}` | {o.get('service_name', '?')[:25]}\n"
                        f"{st} Status: **{status_text}**\n"
                        f"💰 ₹{o.get('charge', 0)} | 📦 Qty: {o.get('quantity', 0)} | {date_str}"
                    )
                txt = "📦 **My SMM Orders:**\n\n" + "\n\n".join(lines)
            await event.edit(txt, buttons=[[Button.inline("🔙 Back", b"smm_services", style="primary")]])
            await event.answer()
            return

        if data == "smm_confirm_order":
            state = user_states.get(user_id)
            if not state or state.get("action") != "smm_order" or state.get("step") != "confirm":
                await event.answer("No pending order.", alert=True)
                return

            service = state["service"]
            link = state["link"]
            quantity = state["quantity"]
            charge = state["charge"]

            # Atomically reserve the charge FIRST — prevents both double-spend
            # (e.g. fast double-tap) and placing a paid order without payment.
            deduct_result = await users_col.update_one(
                {"user_id": user_id, "balance": {"$gte": charge}},
                {"$inc": {"balance": -charge}}
            )
            if deduct_result.modified_count == 0:
                await event.answer("❌ Insufficient balance.", alert=True)
                user_states.pop(user_id, None)
                return

            try:
                async with aiohttp.ClientSession() as s:
                    async with s.post(SMM_API_URL, data={
                        "key": SMM_API_KEY,
                        "action": "add",
                        "service": service["service"],
                        "link": link,
                        "quantity": quantity,
                    }) as r:
                        result = await r.json(content_type=None)
            except Exception as e:
                # Refund since the order was never placed.
                await users_col.update_one({"user_id": user_id}, {"$inc": {"balance": charge}})
                await event.edit(f"❌ Order failed: {e}\n\n💰 Your ₹{charge} has been refunded.",
                                  buttons=[[Button.inline("🔙 Back", b"smm_services", style="primary")]])
                await event.answer()
                fail_name = await get_display_name(user_id)
                await log_event(
                    f"❌ **SMM Order Failed (API Error)**\n"
                    f"👤 User: {fail_name} (`{user_id}`)\n"
                    f"📦 Service: {service['name']} (`{service['service']}`)\n"
                    f"⚠️ Error: {e}\n"
                    f"💰 Refunded: ₹{charge}\n"
                    f"🕐 Time: {now_ist().strftime('%d/%m/%Y %H:%M:%S')} IST"
                )
                user_states.pop(user_id, None)
                return

            if not isinstance(result, dict) or "order" not in result:
                err = result.get("error", "Unknown error") if isinstance(result, dict) else "Unknown error"
                # Refund since the panel did not accept the order.
                await users_col.update_one({"user_id": user_id}, {"$inc": {"balance": charge}})
                await event.edit(f"❌ SMM panel rejected the order: {err}\n\n💰 Your ₹{charge} has been refunded.",
                                  buttons=[[Button.inline("🔙 Back", b"smm_services", style="primary")]])
                await event.answer()
                fail_name = await get_display_name(user_id)
                await log_event(
                    f"❌ **SMM Order Rejected (Panel Error)**\n"
                    f"👤 User: {fail_name} (`{user_id}`)\n"
                    f"📦 Service: {service['name']} (`{service['service']}`)\n"
                    f"⚠️ Panel Error: {err}\n"
                    f"💰 Refunded: ₹{charge}\n"
                    f"🕐 Time: {now_ist().strftime('%d/%m/%Y %H:%M:%S')} IST"
                )
                user_states.pop(user_id, None)
                return

            await smm_orders_col.insert_one({
                "user_id": user_id,
                "service_id": service["service"],
                "service_name": service["name"],
                "category": service.get("category", "Other"),
                "link": link,
                "quantity": quantity,
                "charge": charge,
                "smm_order_id": result["order"],
                "status": "pending",
                "created_at": now_ist(),
            })
            user_states.pop(user_id, None)

            smm_buyer_name = await get_display_name(user_id)
            updated_user = await users_col.find_one({"user_id": user_id})
            new_bal = updated_user["balance"] if updated_user else 0
            await log_event(
                f"🚀 **New SMM Order**\n"
                f"👤 User: {smm_buyer_name} (`{user_id}`)\n"
                f"🆔 SMM Order ID: `{result['order']}`\n"
                f"📦 Service: {service['name']} (`{service['service']}`)\n"
                f"🗂️ Category: {service.get('category', 'Other')}\n"
                f"🔗 Link: {link}\n"
                f"📊 Quantity: {quantity}\n"
                f"💰 Charged: ₹{charge} | 👛 Balance After: ₹{new_bal}\n"
                f"🕐 Time: {now_ist().strftime('%d/%m/%Y %H:%M:%S')} IST"
            )
            await event.edit(
                f"✅ **Order placed!**\n\n"
                f"🆔 Order ID: `{result['order']}`\n"
                f"📦 Service: {service['name']}\n"
                f"🔗 Link: {link}\n"
                f"📊 Quantity: {quantity}\n"
                f"💰 Charged: ₹{charge}",
                buttons=[[Button.inline("🔙 Back to Menu", b"main", style="primary")]]
            )
            await event.answer()
            return

        if data == "smm_cancel_order":
            user_states.pop(user_id, None)
            await event.edit("❌ Order cancelled.", buttons=[[Button.inline("🔙 Back", b"smm_services", style="primary")]])
            await event.answer()
            return

        # ---------- ADMIN PANEL ----------
        if data == "admin":
            if user_id not in ADMIN_IDS:
                await event.answer("❌ Unauthorized", alert=True)
                return
            btns = [
                [Button.inline("📦 Accounts & Stock", b"admin_cat_accounts", style="primary")],
                [Button.inline("💰 Finance & Transactions", b"admin_cat_finance", style="primary")],
                [Button.inline("🚀 SMM Panel", b"admin_cat_smm", style="success")],
                [Button.inline("⚙️ Bot Settings", b"admin_cat_settings", style="primary")],
                [Button.inline("🔙 Back", b"main", style="primary")],
            ]
            await event.edit("⚙️ **Admin Panel**\n\nChoose a category:", buttons=btns)
            await event.answer()
            return

        if data == "admin_cat_accounts":
            if user_id not in ADMIN_IDS:
                await event.answer("❌ Unauthorized", alert=True)
                return
            btns = [
                [Button.inline("➕ Add Account (OTP)", b"admin_add_otp", style="success")],
                [Button.inline("📥 Add Account (Session)", b"admin_add_sess", style="success")],
                [Button.inline("📦 Add Accounts to Stock", b"admin_add_stock", style="success")],
                [Button.inline("📋 Accounts (List)", b"admin_accounts", style="primary")],
                [Button.inline("🔙 Back to Admin Menu", b"admin", style="primary")],
            ]
            await event.edit("📦 **Accounts & Stock**", buttons=btns)
            await event.answer()
            return

        if data == "admin_cat_finance":
            if user_id not in ADMIN_IDS:
                await event.answer("❌ Unauthorized", alert=True)
                return
            btns = [
                [Button.inline("💰 Add Balance", b"admin_addbal", style="success")],
                [Button.inline("💲 Set Price", b"admin_setprice", style="primary")],
                [Button.inline("🕒 Pending Deposits", b"admin_deposits", style="primary")],
                [Button.inline("📜 Transaction History", b"admin_transactions", style="primary")],
                [Button.inline("📜 Withdrawal History", b"admin_withdrawals", style="primary")],
                [Button.inline("💸 Set Min Withdrawal", b"admin_minwithdraw", style="primary")],
                [Button.inline("🎁 Referral Bonus Settings", b"admin_referral_settings", style="primary")],
                [Button.inline("🔙 Back to Admin Menu", b"admin", style="primary")],
            ]
            await event.edit("💰 **Finance & Transactions**", buttons=btns)
            await event.answer()
            return

        if data == "admin_referral_settings":
            if user_id not in ADMIN_IDS:
                await event.answer("❌ Unauthorized", alert=True)
                return
            cur_percent = await get_referral_bonus_percent()
            cur_max = await get_referral_bonus_max()
            btns = [
                [Button.inline("✏️ Edit Bonus %", b"admin_set_ref_percent", style="primary")],
                [Button.inline("✏️ Edit Max Cap (₹)", b"admin_set_ref_max", style="primary")],
                [Button.inline("🔙 Back to Finance Menu", b"admin_cat_finance", style="primary")],
            ]
            await event.edit(
                f"🎁 **Referral Bonus Settings**\n\n"
                f"Current: **{cur_percent}%** of first deposit, capped at **₹{cur_max}**\n\n"
                f"Example: A ₹100 deposit currently pays a "
                f"₹{round(min(100 * (cur_percent/100), cur_max), 2)} referral bonus.",
                buttons=btns
            )
            await event.answer()
            return

        if data == "admin_set_ref_percent":
            if user_id not in ADMIN_IDS:
                await event.answer("❌ Unauthorized", alert=True)
                return
            user_states[user_id] = {"action": "set_ref_percent", "step": "await_value"}
            await event.edit(
                "📈 Send new referral bonus **percentage** (e.g. `10` for 10%):",
                buttons=[[Button.inline("🔙 Cancel", b"admin_referral_settings", style="primary")]]
            )
            await event.answer()
            return

        if data == "admin_set_ref_max":
            if user_id not in ADMIN_IDS:
                await event.answer("❌ Unauthorized", alert=True)
                return
            user_states[user_id] = {"action": "set_ref_max", "step": "await_value"}
            await event.edit(
                "💸 Send new referral bonus **max cap** in ₹ (e.g. `5`):",
                buttons=[[Button.inline("🔙 Cancel", b"admin_referral_settings", style="primary")]]
            )
            await event.answer()
            return

        if data == "admin_cat_smm":
            if user_id not in ADMIN_IDS:
                await event.answer("❌ Unauthorized", alert=True)
                return
            btns = [
                [Button.inline("🚀 SMM Order History", b"admin_smm_orders", style="primary")],
                [Button.inline("📈 Set SMM Markup", b"admin_smm_markup", style="primary")],
                [Button.inline("🔙 Back to Admin Menu", b"admin", style="primary")],
            ]
            await event.edit("🚀 **SMM Panel**", buttons=btns)
            await event.answer()
            return

        if data == "admin_cat_settings":
            if user_id not in ADMIN_IDS:
                await event.answer("❌ Unauthorized", alert=True)
                return
            btns = [
                [Button.inline("📞 Set Support Link", b"admin_support", style="primary")],
                [Button.inline("🔙 Back to Admin Menu", b"admin", style="primary")],
            ]
            await event.edit("⚙️ **Bot Settings**", buttons=btns)
            await event.answer()
            return

        # ---------- ADMIN ADD ACCOUNTS TO STOCK (BULK) ----------
        if data == "admin_add_stock":
            user_states[user_id] = {"action": "add_stock", "step": "await_bulk"}
            await event.edit(
                "➕ **Add Accounts to Stock**\n\n"
                "Send in this format:\n\n"
                "`CountryName|price|2FA`\n"
                "`account_data_line_1`\n"
                "`account_data_line_2`\n"
                "`....`\n\n"
                "**Example:**\n"
                "`India|55|real1`\n"
                "`session_string_1`\n"
                "`session_string_2`\n"
                "`....`\n\n"
                "First line = country|price|2FA (2FA password, send `none` if accounts have no 2FA), "
                "every line after that = one session string (one account per line).",
                buttons=[[Button.inline("🔙 Cancel", b"admin", style="danger")]]
            )
            await event.answer()
            return

        # ---------- OTHER ADMIN HANDLERS ----------
        if data == "admin_add_otp":
            await start_add_phone_flow(event)
            await event.answer()
            return
        if data == "admin_add_sess":
            await start_add_session_flow(event)
            await event.answer()
            return
        if data == "admin_addbal":
            user_states[user_id] = {"action": "add_balance", "step": "await_user_id"}
            await event.edit("👤 Send user ID:", buttons=[[Button.inline("🔙 Cancel", b"admin", style="danger")]])
            await event.answer()
            return
        if data == "admin_deposits":
            cursor = deposits_col.find({"status": "pending"}).sort("created_at", 1)
            pending = await cursor.to_list(length=10)
            if not pending:
                await event.answer("No pending deposits.", alert=True)
                return
            btns = []
            for dep in pending:
                txn_id = dep.get('txn_id', 'N/A')
                btns.append([
                    Button.inline(f"✅ Approve ₹{dep['amount']} ({txn_id})", f"approve_{dep['_id']}", style="success"),
                    Button.inline(f"❌ Reject", f"reject_{dep['_id']}", style="danger")
                ])
            btns.append([Button.inline("🔙 Back", b"admin", style="primary")])
            await event.edit("🕒 **Pending Deposits**", buttons=btns)
            await event.answer()
            return
        if data.startswith("approve_"):
            dep_id = data.split("_", 1)[1]
            deposit = await deposits_col.find_one({"_id": ObjectId(dep_id)})
            if not deposit or deposit["status"] != "pending":
                await event.answer("Already processed.", alert=True)
                return
            user_id_dep = deposit["user_id"]
            amount = deposit["amount"]
            await deposits_col.update_one({"_id": ObjectId(dep_id)}, {"$set": {"status": "approved"}})
            await users_col.update_one(
                {"user_id": user_id_dep},
                {"$inc": {"balance": amount}},
                upsert=True
            )
            # ---------- Referral Bonus: 10% of referred user's FIRST deposit, capped ----------
            user_doc = await users_col.find_one({"user_id": user_id_dep})
            referrer_id = user_doc.get("referred_by") if user_doc else None
            bonus_already_paid = user_doc.get("referral_bonus_paid", False) if user_doc else False

            if referrer_id and not bonus_already_paid:
                ref_percent = await get_referral_bonus_percent()
                ref_max = await get_referral_bonus_max()
                bonus = round(min(amount * (ref_percent / 100), ref_max), 2)
                if bonus > 0:
                    await users_col.update_one(
                        {"user_id": referrer_id},
                        {"$inc": {"balance": bonus, "withdrawable_balance": bonus, "referral_earnings": bonus}},
                        upsert=True
                    )
                    await users_col.update_one(
                        {"user_id": user_id_dep},
                        {"$set": {"referral_bonus_paid": True}}
                    )
                    try:
                        await bot.send_message(
                            referrer_id,
                            f"🎉 **Referral Bonus Earned!**\n\n"
                            f"Your referral made their first deposit of ₹{amount}.\n"
                            f"💰 You earned: ₹{bonus} ({ref_percent}% up to ₹{ref_max})"
                        )
                    except Exception:
                        pass
                    referrer_name = await get_display_name(referrer_id)
                    await log_event(
                        f"🎁 **Referral Bonus Paid**\n"
                        f"👤 Referrer: {referrer_name} (`{referrer_id}`)\n"
                        f"👤 Referred User: `{user_id_dep}` (first deposit ₹{amount})\n"
                        f"💰 Bonus: ₹{bonus}\n"
                        f"🕐 {now_ist().strftime('%d/%m/%Y %H:%M:%S')} IST"
                    )
                else:
                    # Still mark as paid so we don't re-check every future deposit
                    await users_col.update_one(
                        {"user_id": user_id_dep},
                        {"$set": {"referral_bonus_paid": True}}
                    )

            admin_name = await get_display_name(user_id)
            orig_msg = await event.get_message()
            original_caption = (orig_msg.text or orig_msg.message or "") if orig_msg else ""
            new_caption = (
                original_caption
                + f"\n\n✅ **APPROVED** by {admin_name}\n"
                + f"🕐 {now_ist().strftime('%d/%m/%Y %H:%M:%S')} IST"
            )
            try:
                await event.edit(new_caption, buttons=None)
            except MessageNotModifiedError:
                pass
            except Exception as e:
                logging.error(f"Could not edit approve message: {e}")
            try:
                await bot.send_message(
                    user_id_dep,
                    f"✅ **Deposit Approved!**\n💰 ₹{amount} added to your balance."
                )
            except Exception:
                pass
            await event.answer("✅ Approved")
            return
        if data.startswith("reject_"):
            dep_id = data.split("_", 1)[1]
            deposit = await deposits_col.find_one({"_id": ObjectId(dep_id)})
            if not deposit or deposit["status"] != "pending":
                await event.answer("Already processed.", alert=True)
                return
            await deposits_col.update_one({"_id": ObjectId(dep_id)}, {"$set": {"status": "rejected"}})
            admin_name = await get_display_name(user_id)
            orig_msg = await event.get_message()
            original_caption = (orig_msg.text or orig_msg.message or "") if orig_msg else ""
            new_caption = (
                original_caption
                + f"\n\n❌ **REJECTED** by {admin_name}\n"
                + f"🕐 {now_ist().strftime('%d/%m/%Y %H:%M:%S')} IST"
            )
            try:
                await event.edit(new_caption, buttons=None)
            except MessageNotModifiedError:
                pass
            except Exception as e:
                logging.error(f"Could not edit reject message: {e}")
            try:
                await bot.send_message(
                    deposit["user_id"],
                    f"❌ **Deposit Rejected.**\nIf you believe this is a mistake, please contact support."
                )
            except Exception:
                pass
            await event.answer("❌ Rejected")
            return
        if data == "admin_setprice":
            if user_id not in ADMIN_IDS:
                await event.answer("❌ Unauthorized", alert=True)
                return
            user_states[user_id] = {"action": "set_price", "step": "await_price"}
            await event.edit("💲 Send new default price (e.g., 50):", buttons=[[Button.inline("🔙 Cancel", b"admin", style="danger")]])
            await event.answer()
            return
        if data == "admin_support":
            if user_id not in ADMIN_IDS:
                await event.answer("❌ Unauthorized", alert=True)
                return
            user_states[user_id] = {"action": "set_support_link", "step": "await_link"}
            await event.edit("📞 Send support link or 'remove':", buttons=[[Button.inline("🔙 Cancel", b"admin", style="danger")]])
            await event.answer()
            return
        if data == "admin_minwithdraw":
            if user_id not in ADMIN_IDS:
                await event.answer("❌ Unauthorized", alert=True)
                return
            user_states[user_id] = {"action": "set_min_withdraw", "step": "await_value"}
            await event.edit("💸 Send new minimum withdrawal amount:", buttons=[[Button.inline("🔙 Cancel", b"admin", style="danger")]])
            await event.answer()
            return
        if data == "admin_smm_markup":
            if user_id not in ADMIN_IDS:
                await event.answer("❌ Unauthorized", alert=True)
                return
            default_m = await get_smm_markup("")
            member_m = await get_smm_markup("Members Subscribers")
            await event.edit(
                f"📈 **SMM Markup Settings**\n\n"
                f"• Default (all other services): **{default_m}x** ({round((default_m-1)*100)}%)\n"
                f"• Members/Subscribers: **{member_m}x** ({round((member_m-1)*100)}%)\n\n"
                "Which one do you want to change?",
                buttons=[
                    [Button.inline("✏️ Edit Default Markup", b"admin_smm_markup_default", style="primary")],
                    [Button.inline("✏️ Edit Member Markup", b"admin_smm_markup_member", style="primary")],
                    [Button.inline("🔙 Back", b"admin", style="primary")],
                ]
            )
            await event.answer()
            return
        if data in ("admin_smm_markup_default", "admin_smm_markup_member"):
            if user_id not in ADMIN_IDS:
                await event.answer("❌ Unauthorized", alert=True)
                return
            is_member = data.endswith("_member")
            label = "Members/Subscribers" if is_member else "Default"
            user_states[user_id] = {"action": "set_smm_markup", "step": "await_value", "member": is_member}
            await event.edit(
                f"📈 Send new **{label}** markup multiplier (e.g. `1.5` = 50% markup):",
                buttons=[[Button.inline("🔙 Cancel", b"admin_smm_markup", style="danger")]]
            )
            await event.answer()
            return

        # ---------- ADD COUNTRY ----------
        if data.startswith("addcountry_"):
            if data == "addcountry_new":
                state = user_states.get(user_id)
                if not state or state.get("action") not in ("add_phone_otp", "add_session"):
                    await event.answer("❌ Session expired. Start again.", alert=True)
                    return
                state["step"] = "country_manual"
                await event.edit("🌍 Send new country code (e.g., IN):", buttons=[[Button.inline("🔙 Cancel", b"admin", style="danger")]])
            else:
                country = data[len("addcountry_"):]
                state = user_states.get(user_id)
                if not state or state.get("action") not in ("add_phone_otp", "add_session"):
                    await event.answer("❌ Session expired. Start again.", alert=True)
                    return
                state["country"] = country
                state["step"] = "price"
                await event.edit("💵 Send price (e.g., 50):", buttons=[[Button.inline("🔙 Cancel", b"admin", style="danger")]])
            await event.answer()
            return

        # ---------- APPROVE / REJECT WITHDRAWAL ----------
        if data.startswith("wapprove_") or data.startswith("wreject_"):
            if user_id not in ADMIN_IDS:
                await event.answer("❌ Unauthorized", alert=True)
                return
            parts = data.split("_", 1)
            action = parts[0]
            w_id = parts[1]
            withdrawal = await withdrawals_col.find_one({"_id": ObjectId(w_id)})
            if not withdrawal or withdrawal["status"] != "pending":
                await event.answer("Already processed.", alert=True)
                return
            if action == "wapprove":
                # Funds were already reserved (deducted) when the request was
                # submitted — approval just confirms it, no further deduction.
                await withdrawals_col.update_one(
                    {"_id": ObjectId(w_id)},
                    {"$set": {"status": "approved", "processed_at": now_ist()}}
                )
                try:
                    await bot.send_message(withdrawal["user_id"], f"✅ Withdrawal of ₹{withdrawal['amount']} approved.")
                except:
                    pass
                await event.edit("✅ Withdrawal approved.", buttons=[[Button.inline("🔙 Admin Menu", b"admin", style="primary")]])
            else:
                # Refund the reserved amount back since the withdrawal did not happen.
                await users_col.update_one(
                    {"user_id": withdrawal["user_id"]},
                    {"$inc": {"balance": withdrawal["amount"], "withdrawable_balance": withdrawal["amount"]}},
                    upsert=True
                )
                await withdrawals_col.update_one(
                    {"_id": ObjectId(w_id)},
                    {"$set": {"status": "rejected", "processed_at": now_ist()}}
                )
                try:
                    await bot.send_message(
                        withdrawal["user_id"],
                        f"❌ Withdrawal of ₹{withdrawal['amount']} rejected. "
                        f"The amount has been refunded to your withdrawable balance."
                    )
                except:
                    pass
                await event.edit("❌ Withdrawal rejected (amount refunded to user).",
                                  buttons=[[Button.inline("🔙 Admin Menu", b"admin", style="primary")]])
            await event.answer()
            return

        # ---------- UNKNOWN ----------
        await event.answer("❓ Unknown action. Use menu buttons.", alert=True)

    except Exception as e:
        logging.error(f"❌ Callback error: {e}", exc_info=True)
        try:
            await event.answer("❌ Something went wrong.", alert=True)
        except:
            pass


# ============================================================
#  3. USER WITHDRAWALS HISTORY (pagination)
# ============================================================

async def show_user_withdrawals(event, user_id):
    try:
        state = user_states.get(user_id, {})
        page = state.get("user_wd_page", 1)
        per_page = 10

        query = {"user_id": user_id}
        total = await withdrawals_col.count_documents(query)
        total_pages = max(1, (total + per_page - 1) // per_page)
        if page > total_pages:
            page = total_pages
            state["user_wd_page"] = page
            user_states[user_id] = state

        skip = (page - 1) * per_page
        cursor = withdrawals_col.find(query).sort("created_at", -1).skip(skip).limit(per_page)
        withdrawals = await cursor.to_list(length=per_page)

        if not withdrawals:
            txt = "💸 **Your Withdrawal History**\n\nNo withdrawals found."
        else:
            lines = []
            for wd in withdrawals:
                status_emoji = {"pending": "🟡", "approved": "🟢", "rejected": "🔴"}.get(wd.get("status"), "❓")
                date_str = wd["created_at"].strftime('%d/%m/%Y %H:%M')
                lines.append(
                    f"{status_emoji} ₹{wd['amount']} | UPI: {wd.get('upi_id','N/A')} | {wd.get('status','unknown')} | {date_str}"
                )
            txt = f"💸 **Your Withdrawal History** – Page {page}/{total_pages}\n" + "\n".join(lines)

        btns = []
        page_row = []
        if page > 1:
            page_row.append(Button.inline("⬅️ Prev", f"user_wd_page_{page-1}", style="primary"))
        if page < total_pages:
            page_row.append(Button.inline("Next ➡️", f"user_wd_page_{page+1}", style="primary"))
        if page_row:
            btns.append(page_row)
        btns.append([Button.inline("🔙 Back to Referral", b"referral_info", style="primary")])

        await event.edit(txt, buttons=btns)
    except Exception as e:
        logging.error(f"Error in show_user_withdrawals: {e}", exc_info=True)
        await event.edit("❌ Error loading withdrawals.", buttons=[[Button.inline("🔙 Back", b"referral_info", style="primary")]])


# ============================================================
#  4. ADD PHONE (OTP) AND SESSION FLOWS (existing)
# ============================================================

async def start_add_phone_flow(event):
    user_states[event.sender_id] = {"action": "add_phone_otp", "step": "phone"}
    await event.edit("📱 Send phone (e.g., +919876543210):", buttons=[[Button.inline("🔙 Cancel", b"admin", style="danger")]])

async def process_phone_otp_step(event):
    user_id = event.sender_id
    state = user_states.get(user_id)
    if not state or state["action"] != "add_phone_otp":
        return
    step = state["step"]
    if step == "phone":
        phone = event.message.text.strip()
        state["phone"] = phone
        temp_client = TelegramClient(StringSession(), API_ID, API_HASH)
        await temp_client.connect()
        try:
            sent = await temp_client.send_code_request(phone)
            state["temp_client"] = temp_client
            state["phone_code_hash"] = sent.phone_code_hash
            state["step"] = "otp"
            await event.respond("✉️ OTP sent! Send code:", buttons=[[Button.inline("🔙 Cancel", b"admin", style="danger")]])
        except Exception as e:
            await temp_client.disconnect()
            await event.respond(f"❌ Error: {str(e)}", buttons=[[Button.inline("🔙 Cancel", b"admin", style="danger")]])
            user_states.pop(user_id, None)
    elif step == "otp":
        code = event.message.text.strip()
        temp_client = state["temp_client"]
        try:
            await temp_client.sign_in(state["phone"], code)
        except SessionPasswordNeededError:
            state["step"] = "2fa"
            await event.respond("🔒 2FA password required. Send password:", buttons=[[Button.inline("🔙 Cancel", b"admin", style="danger")]])
            return
        except Exception as e:
            await temp_client.disconnect()
            await event.respond(f"❌ Login failed: {str(e)}", buttons=[[Button.inline("🔙 Cancel", b"admin", style="danger")]])
            user_states.pop(user_id, None)
            return
        session_str = temp_client.session.save()
        state["session"] = session_str
        state["step"] = "choose_country"
        existing = await get_existing_countries()
        btns = [[Button.inline(c, f"addcountry_{c}", style="primary")] for c in existing]
        btns.append([Button.inline("➕ New Country", b"addcountry_new", style="primary")])
        btns.append([Button.inline("🔙 Cancel", b"admin", style="danger")])
        await temp_client.disconnect()
        await event.respond("🌍 Select country or add new:", buttons=btns)
    elif step == "2fa":
        password = event.message.text.strip()
        temp_client = state["temp_client"]
        try:
            await temp_client.sign_in(password=password)
            session_str = temp_client.session.save()
            state["session"] = session_str
            state["twofa_password"] = password
            state["step"] = "choose_country"
            existing = await get_existing_countries()
            btns = [[Button.inline(c, f"addcountry_{c}", style="primary")] for c in existing]
            btns.append([Button.inline("➕ New Country", b"addcountry_new", style="primary")])
            btns.append([Button.inline("🔙 Cancel", b"admin", style="danger")])
            await temp_client.disconnect()
            await event.respond("🌍 Select country or add new:", buttons=btns)
        except Exception as e:
            await temp_client.disconnect()
            await event.respond(f"❌ 2FA failed: {str(e)}", buttons=[[Button.inline("🔙 Cancel", b"admin", style="danger")]])
            user_states.pop(user_id, None)
    elif step == "country_manual":
        country = event.message.text.strip().upper()
        state["country"] = country
        state["step"] = "price"
        await event.respond("💵 Send price (e.g., 50):", buttons=[[Button.inline("🔙 Cancel", b"admin", style="danger")]])
    elif step == "price":
        try:
            price = float(event.message.text.strip())
            if price <= 0:
                raise ValueError
        except:
            await event.respond("❌ Invalid price. Send positive number:", buttons=[[Button.inline("🔙 Cancel", b"admin", style="danger")]])
            return
        state["price"] = price
        phone = state["phone"]
        country = state["country"]
        session_str = state["session"]
        twofa_password = state.get("twofa_password")
        insert_data = {
            "phone": phone,
            "country": country,
            "session_string": session_str,
            "status": "available",
            "price": price
        }
        if twofa_password:
            insert_data["twofa_password"] = twofa_password
        await accounts_col.insert_one(insert_data)
        await acc_mgr.add_client(phone, session_str)
        await event.respond(f"✅ Account `{phone}` ({country}) added at ₹{price}!", buttons=[[Button.inline("🔙 Admin Menu", b"admin", style="primary")]])
        user_states.pop(user_id, None)

async def start_add_session_flow(event):
    user_states[event.sender_id] = {"action": "add_session", "step": "session"}
    await event.edit("🔑 Send session string:", buttons=[[Button.inline("🔙 Cancel", b"admin", style="danger")]])

async def process_session_step(event):
    user_id = event.sender_id
    state = user_states.get(user_id)
    if not state or state["action"] != "add_session":
        return
    step = state["step"]
    if step == "session":
        session_str = event.message.text.strip()
        state["session_str"] = session_str
        temp_client = TelegramClient(StringSession(session_str), API_ID, API_HASH)
        try:
            await temp_client.connect()
            if not await temp_client.is_user_authorized():
                await temp_client.disconnect()
                await event.respond("❌ Invalid session.", buttons=[[Button.inline("🔙 Admin Menu", b"admin", style="primary")]])
                user_states.pop(user_id, None)
                return
            me = await temp_client.get_me()
            phone = me.phone
            state["phone"] = phone
            state["client"] = temp_client
            state["step"] = "ask_2fa"
            await event.respond(f"📱 Number: {phone}\n\n🔐 2FA password? (send or 'skip'):", buttons=[[Button.inline("🔙 Cancel", b"admin", style="danger")]])
        except Exception as e:
            await temp_client.disconnect()
            await event.respond(f"❌ Error: {str(e)}", buttons=[[Button.inline("🔙 Cancel", b"admin", style="danger")]])
            user_states.pop(user_id, None)
    elif step == "ask_2fa":
        answer = event.message.text.strip()
        if answer.lower() != "skip":
            state["twofa_password"] = answer
        state["step"] = "choose_country"
        existing = await get_existing_countries()
        btns = [[Button.inline(c, f"addcountry_{c}", style="primary")] for c in existing]
        btns.append([Button.inline("➕ New Country", b"addcountry_new", style="primary")])
        btns.append([Button.inline("🔙 Cancel", b"admin", style="danger")])
        await event.respond("🌍 Select country or add new:", buttons=btns)
    elif step == "country_manual":
        country = event.message.text.strip().upper()
        state["country"] = country
        state["step"] = "price"
        await event.respond("💵 Send price (e.g., 50):", buttons=[[Button.inline("🔙 Cancel", b"admin", style="danger")]])
    elif step == "price":
        try:
            price = float(event.message.text.strip())
            if price <= 0:
                raise ValueError
        except:
            await event.respond("❌ Invalid price. Send a positive number:", buttons=[[Button.inline("🔙 Cancel", b"admin", style="danger")]])
            return
        
        phone = state.get("phone")
        country = state.get("country")
        
        # 🔍 DEBUG: print the state to see what's inside
        logging.info(f"[DEBUG] price step - state keys: {list(state.keys())}")
        logging.info(f"[DEBUG] phone: {phone}, country: {country}, session_str: {state.get('session_str')}")
        
        # Try to get session from multiple possible keys
        client = state.get("client")
        session_str = state.get("session_str")
        
        # Fallback: try to get from 'session' key (if any)
        if not session_str and state.get("session"):
            session_str = state["session"]
            logging.info("[DEBUG] Using 'session' key as session_str")
        
        if client:
            new_session = client.session.save()
            await client.disconnect()
        elif session_str:
            new_session = session_str
        else:
            logging.error(f"[ERROR] No session found in state: {state}")
            await event.respond(
                "❌ No session found. Please start again using the 'Add Account (Session File)' option.",
                buttons=[[Button.inline("🔙 Cancel", b"admin", style="danger")]]
            )
            user_states.pop(user_id, None)
            return
        
        twofa_password = state.get("twofa_password")
        insert_data = {
            "phone": phone,
            "country": country,
            "session_string": new_session,
            "status": "available",
            "price": price
        }
        if twofa_password:
            insert_data["twofa_password"] = twofa_password
        await accounts_col.insert_one(insert_data)
        await acc_mgr.add_client(phone, new_session)
        await event.respond(f"✅ Account `{phone}` ({country}) added at ₹{price}!", buttons=[[Button.inline("🔙 Admin Menu", b"admin", style="primary")]])
        user_states.pop(user_id, None)


# ============================================================
#  5. BULK ADD ACCOUNTS TO STOCK
# ============================================================

async def process_add_stock_step(event):
    user_id = event.sender_id
    state = user_states.get(user_id)
    if not state or state["action"] != "add_stock":
        return

    raw_text = event.message.text or ""
    lines = [ln.strip() for ln in raw_text.splitlines() if ln.strip()]

    if len(lines) < 2:
        await event.respond(
            "❌ Invalid format. Send the header line and at least one session string.\n\n"
            "Example:\n`India|55|real1`\n`session_string_1`",
            buttons=[[Button.inline("🔙 Cancel", b"admin", style="danger")]]
        )
        return

    header = lines[0]
    parts = [p.strip() for p in header.split("|")]
    if len(parts) < 2:
        await event.respond(
            "❌ Invalid header. Use format: `CountryName|price|2FA`",
            buttons=[[Button.inline("🔙 Cancel", b"admin", style="danger")]]
        )
        return

    country = parts[0].upper()
    try:
        price = float(parts[1])
        if price <= 0:
            raise ValueError
    except ValueError:
        await event.respond(
            "❌ Invalid price in header. Use format: `CountryName|price|2FA`",
            buttons=[[Button.inline("🔙 Cancel", b"admin", style="danger")]]
        )
        return
    twofa = parts[2] if len(parts) > 2 and parts[2].lower() not in ("", "none", "skip") else None

    session_lines = lines[1:]
    total = len(session_lines)

    status_msg = await event.respond(f"⏳ Processing 0/{total} accounts...")

    added, failed, duplicates = 0, 0, 0
    fail_reasons = []

    for idx, session_str in enumerate(session_lines, start=1):
        temp_client = TelegramClient(StringSession(session_str), API_ID, API_HASH)
        try:
            await temp_client.connect()
            if not await temp_client.is_user_authorized():
                await temp_client.disconnect()
                failed += 1
                fail_reasons.append(f"Line {idx}: invalid/unauthorized session")
                continue

            me = await temp_client.get_me()
            phone = me.phone
            new_session = temp_client.session.save()
            await temp_client.disconnect()

            existing = await accounts_col.find_one({"phone": phone})
            if existing and existing.get("status") == "available":
                duplicates += 1
                continue

            insert_data = {
                "phone": phone,
                "country": country,
                "session_string": new_session,
                "status": "available",
                "price": price
            }
            if twofa:
                insert_data["twofa_password"] = twofa

            if existing:
                # Existing account but not currently available (e.g. sold/used) -> restock it
                await accounts_col.update_one({"_id": existing["_id"]}, {"$set": insert_data})
            else:
                await accounts_col.insert_one(insert_data)
            await acc_mgr.add_client(phone, new_session)
            added += 1
        except Exception as e:
            try:
                await temp_client.disconnect()
            except Exception:
                pass
            failed += 1
            fail_reasons.append(f"Line {idx}: {str(e)}")

        if idx % 5 == 0 or idx == total:
            try:
                await status_msg.edit(f"⏳ Processing {idx}/{total} accounts...")
            except Exception:
                pass

    summary = (
        f"✅ **Stock Add Complete** ({country} @ ₹{price}{' | 2FA set' if twofa else ' | no 2FA'})\n\n"
        f"➕ Added: {added}\n"
        f"♻️ Duplicates skipped: {duplicates}\n"
        f"❌ Failed: {failed}"
    )
    if fail_reasons:
        shown = "\n".join(fail_reasons[:10])
        summary += f"\n\n**Failure details:**\n{shown}"
        if len(fail_reasons) > 10:
            summary += f"\n...and {len(fail_reasons) - 10} more."

    await event.respond(summary, buttons=[[Button.inline("🔙 Admin Menu", b"admin", style="primary")]])
    admin_name = await get_display_name(user_id)
    await log_event(
        f"📦 **Stock Added**\n"
        f"👤 Admin: {admin_name} (`{user_id}`)\n"
        f"🌍 Country: {country}\n"
        f"💰 Price: ₹{price} {'| 2FA set' if twofa else '| no 2FA'}\n"
        f"➕ Added: {added} | ♻️ Duplicates: {duplicates} | ❌ Failed: {failed}\n"
        f"🕐 Time: {now_ist().strftime('%d/%m/%Y %H:%M:%S')} IST"
    )
    user_states.pop(user_id, None)


# ============================================================
#  7. DEPOSIT FLOW
# ============================================================

async def process_deposit_step(event):
    user_id = event.sender_id
    state = user_states.get(user_id)
    if not state or state["action"] != "deposit":
        return
    step = state["step"]
    if step == "amount":
        try:
            amount = float(event.message.text.strip())
            if amount <= 0 or amount < MIN_DEPOSIT:
                raise ValueError
        except:
            await event.respond(f"❌ Invalid. Min ₹{MIN_DEPOSIT}.", buttons=[[Button.inline("🔙 Cancel", b"main", style="danger")]])
            return
        state["amount"] = amount
        txn_id = f"DEP{datetime.now().strftime('%y%m%d%H%M')}{random.randint(1000,9999)}"
        state["txn_id"] = txn_id

        upi_string = f"upi://pay?pa={UPI_ID}&pn={PAYEE_NAME}&am={amount}&tn={txn_id}"
        img = qrcode.make(upi_string)
        buf = io.BytesIO()
        img.save(buf, format='PNG')
        buf.seek(0)
        buf.name = "qr_code.png"
        caption = (
            f"💳 **Deposit ₹{amount}**\n"
            f"🔑 **Txn ID:** `{txn_id}`\n\n"
            f"📌 **Mention this Txn ID in payment note.**\n\n"
            f"Scan QR or use UPI: `{UPI_ID}`\n\n"
            "Send screenshot after payment."
        )
        await bot.send_file(event.chat_id, buf, caption=caption, buttons=[[Button.inline("🔙 Cancel", b"main", style="danger")]])
        state["step"] = "screenshot"
    elif step == "screenshot":
        if not event.message.photo:
            await event.respond("❌ Please send a photo (screenshot).", buttons=[[Button.inline("🔙 Cancel", b"main", style="danger")]])
            return
        amount = state["amount"]
        txn_id = state.get("txn_id", "N/A")
        result = await deposits_col.insert_one({
            "user_id": user_id,
            "amount": amount,
            "txn_id": txn_id,
            "proof_type": "screenshot",
            "status": "pending",
            "created_at": now_ist()
        })
        dep_id = result.inserted_id
        photo_bytes = await event.message.download_media(file=bytes)
        photo_io = io.BytesIO(photo_bytes)
        photo_io.name = "payment_proof.jpg"
        for admin in ADMIN_IDS:
            try:
                await bot.send_file(admin, photo_io,
                    caption=f"🔔 **New Deposit Request**\nUser: `{user_id}`\nAmount: ₹{amount}\nTxn ID: `{txn_id}`",
                    buttons=[
                        [Button.inline("✅ Approve", f"approve_{dep_id}", style="success"),
                         Button.inline("❌ Reject", f"reject_{dep_id}", style="danger")]
                    ])
                photo_io.seek(0)
            except:
                pass
        await event.respond(f"✅ Deposit request submitted! Amount: ₹{amount}, Txn ID: `{txn_id}`", buttons=[[Button.inline("🔙 Main Menu", b"main", style="primary")]])
        user_states.pop(user_id, None)
        dep_name = await get_display_name(user_id)
        await log_event(
            f"💳 **Deposit Request**\n"
            f"👤 User: {dep_name} (`{user_id}`)\n"
            f"💰 Amount: ₹{amount}\n"
            f"🧾 Txn ID: `{txn_id}`\n"
            f"🆔 Deposit ID: `{dep_id}`\n"
            f"🕐 Time: {now_ist().strftime('%d/%m/%Y %H:%M:%S')} IST"
        )


# ============================================================
#  8. HANDLE ALL TEXT MESSAGES (including new pyrogram flow)
# ============================================================

@bot.on(events.NewMessage(func=lambda e: e.is_private and not e.message.text.startswith('/')))
async def handle_message(event):
    user_id = event.sender_id
    if not await is_user_member(user_id):
        await send_join_message(event)
        return
    state = user_states.get(user_id)
    if not state:
        await send_main_menu(event)
        return
    action = state.get("action")

    # ---- BULK ADD ACCOUNTS TO STOCK ----
    if action == "add_stock":
        await process_add_stock_step(event)
        return

    # ---- OTHER FLOWS ----
    if action == "add_phone_otp":
        await process_phone_otp_step(event)
    elif action == "add_session":
        await process_session_step(event)
    elif action == "add_balance":
        step = state["step"]
        if step == "await_user_id":
            try:
                uid = int(event.message.text.strip())
            except:
                await event.respond("❌ Invalid ID.", buttons=[[Button.inline("🔙 Cancel", b"admin", style="danger")]])
                return
            state["uid"] = uid
            state["step"] = "await_amount"
            await event.respond("💵 Send amount to add:", buttons=[[Button.inline("🔙 Cancel", b"admin", style="danger")]])
        elif step == "await_amount":
            try:
                amt = float(event.message.text.strip())
            except:
                await event.respond("❌ Invalid amount.", buttons=[[Button.inline("🔙 Cancel", b"admin", style="danger")]])
                return
            uid = state["uid"]
            await users_col.update_one(
                {"user_id": uid},
                {"$inc": {"balance": amt}, "$setOnInsert": {"joined_at": now_ist()}},
                upsert=True
            )
            await event.respond(f"✅ Added ₹{amt} to user `{uid}`.", buttons=[[Button.inline("🔙 Admin Menu", b"admin", style="primary")]])
            user_states.pop(user_id, None)
    elif action == "deposit":
        await process_deposit_step(event)
    elif action == "withdraw":
        step = state.get("step")
        if step == "amount":
            try:
                amount = float(event.message.text.strip())
                if amount <= 0:
                    raise ValueError
            except:
                await event.respond("❌ Invalid amount.", buttons=[[Button.inline("🔙 Cancel", b"referral_info", style="danger")]])
                return
            user_doc = await users_col.find_one({"user_id": user_id})
            withdrawable = user_doc.get('withdrawable_balance', 0) if user_doc else 0
            if amount > withdrawable:
                await event.respond(f"❌ You have ₹{withdrawable} only.", buttons=[[Button.inline("🔙 Cancel", b"referral_info", style="danger")]])
                return
            min_wd = await get_min_withdrawal()
            if amount < min_wd:
                await event.respond(f"❌ Minimum withdrawal ₹{min_wd}.", buttons=[[Button.inline("🔙 Cancel", b"referral_info", style="danger")]])
                return
            state["amount"] = amount
            state["step"] = "upi"
            await event.respond("💳 Enter UPI ID (e.g., example@upi):", buttons=[[Button.inline("🔙 Cancel", b"referral_info", style="danger")]])
        elif step == "upi":
            upi = event.message.text.strip()
            if not upi or "@" not in upi:
                await event.respond("❌ Invalid UPI. Try again:", buttons=[[Button.inline("🔙 Cancel", b"referral_info", style="danger")]])
                return
            amount = state["amount"]

            # Atomically reserve the funds right now so the same balance can't be
            # used for another withdrawal request or a purchase while this one
            # is pending. Refunded automatically if the admin rejects it.
            reserve_result = await users_col.update_one(
                {"user_id": user_id, "withdrawable_balance": {"$gte": amount}},
                {"$inc": {"balance": -amount, "withdrawable_balance": -amount}}
            )
            if reserve_result.modified_count == 0:
                await event.respond(
                    "❌ Insufficient withdrawable balance (it may have changed). "
                    "Please check your balance and try again.",
                    buttons=[[Button.inline("🔙 Referral Info", b"referral_info", style="primary")]]
                )
                user_states.pop(user_id, None)
                return

            result = await withdrawals_col.insert_one({
                "user_id": user_id,
                "amount": amount,
                "upi_id": upi,
                "status": "pending",
                "created_at": now_ist()
            })
            w_id = result.inserted_id
            for admin in ADMIN_IDS:
                try:
                    await bot.send_message(admin,
                        f"🔔 **Withdrawal Request**\nUser: `{user_id}`\nAmount: ₹{amount}\nUPI: `{upi}`",
                        buttons=[
                            [Button.inline("✅ Approve", f"wapprove_{w_id}", style="success"),
                             Button.inline("❌ Reject", f"wreject_{w_id}", style="danger")]
                        ]
                    )
                except:
                    pass
            await event.respond(f"✅ Withdrawal of ₹{amount} submitted.", buttons=[[Button.inline("🔙 Referral Info", b"referral_info", style="primary")]])
            wd_name = await get_display_name(user_id)
            await log_event(
                f"💸 **Withdrawal Request**\n"
                f"👤 User: {wd_name} (`{user_id}`)\n"
                f"💰 Amount: ₹{amount}\n"
                f"🏦 UPI: `{upi}`\n"
                f"🆔 Withdrawal ID: `{w_id}`\n"
                f"🕐 Time: {now_ist().strftime('%d/%m/%Y %H:%M:%S')} IST"
            )
            user_states.pop(user_id, None)
    elif action == "set_support_link":
        step = state.get("step")
        if step == "await_link":
            link = event.message.text.strip()
            if link.lower() == "remove":
                await settings_col.delete_one({"key": "support_link"})
                await event.respond("✅ Removed.", buttons=[[Button.inline("🔙 Admin Menu", b"admin", style="primary")]])
            else:
                await set_support_link(link)
                await event.respond(f"✅ Updated to `{link}`.", buttons=[[Button.inline("🔙 Admin Menu", b"admin", style="primary")]])
            user_states.pop(user_id, None)
    elif action == "set_price":
        step = state.get("step")
        if step == "await_price":
            try:
                new_price = float(event.message.text.strip())
                if new_price <= 0:
                    raise ValueError
            except:
                await event.respond("❌ Invalid price.", buttons=[[Button.inline("🔙 Cancel", b"admin", style="danger")]])
                return
            await settings_col.update_one(
                {"key": "default_price"},
                {"$set": {"value": new_price, "updated_at": now_ist()}},
                upsert=True
            )
            global DEFAULT_PRICE
            DEFAULT_PRICE = new_price
            await event.respond(f"✅ Default price ₹{new_price}.", buttons=[[Button.inline("🔙 Admin Menu", b"admin", style="primary")]])
            user_states.pop(user_id, None)
    elif action == "set_min_withdraw":
        step = state.get("step")
        if step == "await_value":
            try:
                val = float(event.message.text.strip())
                if val <= 0:
                    raise ValueError
            except:
                await event.respond("❌ Invalid amount.", buttons=[[Button.inline("🔙 Cancel", b"admin", style="danger")]])
                return
            await set_min_withdrawal(val)
            await event.respond(f"✅ Min withdrawal ₹{val}.", buttons=[[Button.inline("🔙 Admin Menu", b"admin", style="primary")]])
            user_states.pop(user_id, None)

    elif action == "set_smm_markup":
        step = state.get("step")
        if step == "await_value":
            try:
                val = float(event.message.text.strip())
                if val <= 0:
                    raise ValueError
            except:
                await event.respond("❌ Invalid multiplier. Send a number like `1.2`.",
                                     buttons=[[Button.inline("🔙 Cancel", b"admin_smm_markup", style="danger")]])
                return
            is_member = state.get("member", False)
            await set_smm_markup(val, member=is_member)
            label = "Members/Subscribers" if is_member else "Default"
            await event.respond(f"✅ {label} SMM markup set to {val}x.",
                                 buttons=[[Button.inline("🔙 Admin Menu", b"admin", style="primary")]])
            user_states.pop(user_id, None)

    elif action == "set_ref_percent":
        step = state.get("step")
        if step == "await_value":
            try:
                val = float(event.message.text.strip())
                if val < 0 or val > 100:
                    raise ValueError
            except:
                await event.respond("❌ Invalid percentage. Send a number between 0-100, e.g. `10`.",
                                     buttons=[[Button.inline("🔙 Cancel", b"admin_referral_settings", style="danger")]])
                return
            await set_referral_bonus_percent(val)
            await event.respond(f"✅ Referral bonus percentage set to {val}%.",
                                 buttons=[[Button.inline("🔙 Referral Settings", b"admin_referral_settings", style="primary")]])
            user_states.pop(user_id, None)

    elif action == "set_ref_max":
        step = state.get("step")
        if step == "await_value":
            try:
                val = float(event.message.text.strip())
                if val <= 0:
                    raise ValueError
            except:
                await event.respond("❌ Invalid amount. Send a number like `5`.",
                                     buttons=[[Button.inline("🔙 Cancel", b"admin_referral_settings", style="danger")]])
                return
            await set_referral_bonus_max(val)
            await event.respond(f"✅ Referral bonus max cap set to ₹{val}.",
                                 buttons=[[Button.inline("🔙 Referral Settings", b"admin_referral_settings", style="primary")]])
            user_states.pop(user_id, None)

    elif action == "smm_search":
        step = state.get("step")
        if step == "query":
            query = event.message.text.strip()
            if len(query) < 2:
                await event.respond("❌ Please send at least 2 characters to search.",
                                     buttons=[[Button.inline("🔙 Cancel", b"smm_services", style="danger")]])
                return
            usd = await get_usd_inr()
            text, btns = await build_smm_search_results(query, usd)
            user_states.pop(user_id, None)
            await event.respond(text, buttons=btns)
            return

    elif action == "smm_order":
        step = state.get("step")

        if step == "service_id":
            sid_text = event.message.text.strip()
            if not sid_text.isdigit():
                await event.respond("❌ Invalid Service ID. Send numeric ID only.",
                                     buttons=[[Button.inline("🔙 Cancel", b"smm_services", style="danger")]])
                return
            sid = int(sid_text)
            if not _smm_all_services:
                await fetch_smm_services()
            service = next((s for s in _smm_all_services if str(s.get("service")) == str(sid)), None)
            if not service:
                await event.respond("❌ Service ID not found.",
                                     buttons=[[Button.inline("🔙 Cancel", b"smm_services", style="danger")]])
                return
            state["service"] = service
            state["step"] = "link"
            await event.respond(
                f"📦 **{service['name']}**\n"
                f"Min: {service['min']} | Max: {service['max']}\n\n"
                "🔗 Now send the **link** (post/profile/video URL) for this order:",
                buttons=[[Button.inline("🔙 Cancel", b"smm_services", style="danger")]]
            )
            return

        if step == "link":
            link = event.message.text.strip()
            if not link.startswith("http"):
                await event.respond("❌ Please send a valid link starting with http/https.",
                                     buttons=[[Button.inline("🔙 Cancel", b"smm_services", style="danger")]])
                return
            state["link"] = link
            state["step"] = "quantity"
            service = state["service"]
            await event.respond(
                f"📊 Send the **quantity** you want (Min: {service['min']}, Max: {service['max']}):",
                buttons=[[Button.inline("🔙 Cancel", b"smm_services", style="danger")]]
            )
            return

        if step == "quantity":
            service = state["service"]
            try:
                qty = int(event.message.text.strip())
            except:
                await event.respond("❌ Invalid quantity. Send a number.",
                                     buttons=[[Button.inline("🔙 Cancel", b"smm_services", style="danger")]])
                return
            min_q, max_q = int(float(service["min"])), int(float(service["max"]))
            if qty < min_q or qty > max_q:
                await event.respond(f"❌ Quantity must be between {min_q} and {max_q}.",
                                     buttons=[[Button.inline("🔙 Cancel", b"smm_services", style="danger")]])
                return

            usd = await get_usd_inr()
            markup = await get_smm_markup(service.get("category", ""))
            charge = round((float(service["rate"]) / 1000) * qty * usd * markup, 2)

            user = await users_col.find_one({"user_id": user_id})
            balance = user["balance"] if user else 0

            state["quantity"] = qty
            state["charge"] = charge
            state["step"] = "confirm"

            text = (
                f"🧾 **Order Summary**\n\n"
                f"📦 Service: {service['name']}\n"
                f"🔗 Link: {state['link']}\n"
                f"📊 Quantity: {qty}\n"
                f"💰 Charge: ₹{charge}\n"
                f"👛 Your balance: ₹{balance}\n\n"
                + ("✅ Confirm to place the order." if balance >= charge
                   else "❌ Insufficient balance — please deposit first.")
            )
            buttons = [[Button.inline("✅ Confirm Order", b"smm_confirm_order", style="success")],
                       [Button.inline("❌ Cancel", b"smm_cancel_order", style="danger")]]
            await event.respond(text, buttons=buttons)
            return

    else:
        await send_main_menu(event)


# ---------- /start ----------
@bot.on(events.NewMessage(pattern='/start'))
async def start_cmd(event):
    user_id = event.sender_id
    args = event.message.text.split()
    referrer_id = None
    if len(args) > 1 and args[1].startswith('ref'):
        try:
            referrer_id = int(args[1][3:])
        except:
            pass
    user_data = await users_col.find_one({"user_id": user_id})
    if not user_data:
        await users_col.insert_one({
            "user_id": user_id,
            "balance": 0,
            "joined_at": now_ist(),
            "referred_by": referrer_id,
            "referral_bonus_paid": False,
            "withdrawable_balance": 0
        })
    else:
        if user_data.get("referred_by") is None and referrer_id and referrer_id != user_id:
            await users_col.update_one({"user_id": user_id}, {"$set": {"referred_by": referrer_id}})
        if "referral_bonus_paid" not in user_data:
            await users_col.update_one({"user_id": user_id}, {"$set": {"referral_bonus_paid": False}})
        if "withdrawable_balance" not in user_data:
            await users_col.update_one({"user_id": user_id}, {"$set": {"withdrawable_balance": 0}})

    if not await is_user_member(user_id):
        await send_join_message(event)
        return

    await show_welcome_menu(event, user_id)


# ---------- MAIN ----------
async def main():
    if not BOT_TOKEN:
        logging.error("❌ BOT_TOKEN missing! Exiting.")
        sys.exit(1)

    masked = BOT_TOKEN[:6] + "..." + BOT_TOKEN[-4:] if len(BOT_TOKEN) > 12 else "***"
    logging.info(f"Starting with BOT_TOKEN={masked} (len={len(BOT_TOKEN)}), cwd={os.getcwd()}")

    try:
        await bot.connect()
    except Exception as e:
        logging.error(f"❌ Could not connect to Telegram: {e}")
        sys.exit(1)

    try:
        if not await bot.is_user_authorized():
            # Explicit bot sign-in only — never falls back to interactive
            # phone/token input() the way client.start() can on a flaky env.
            await bot.sign_in(bot_token=BOT_TOKEN)
    except AccessTokenInvalidError:
        logging.error("❌ Invalid BOT_TOKEN! Check your .env value against @BotFather.")
        sys.exit(1)
    except Exception as e:
        logging.error(f"❌ Sign-in error: {e}")
        sys.exit(1)

    global acc_mgr
    acc_mgr = AccountManager(accounts_col, bot, API_ID, API_HASH, pending_otp_requests, ADMIN_IDS)
    await acc_mgr.load_all()
    logging.info("🚀 Bot started successfully...")
    await bot.run_until_disconnected()

if __name__ == '__main__':
    asyncio.run(main())