import os
import io
import asyncio
import logging
import random
from datetime import datetime
from dotenv import load_dotenv
from telethon import TelegramClient, events, Button, functions
from telethon.sessions import StringSession
from telethon.errors import (
    SessionPasswordNeededError,
    UserNotParticipantError,
    ChatAdminRequiredError,
    ChannelPrivateError,
    AccessTokenInvalidError
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
REFERRAL_BONUS = float(os.getenv("REFERRAL_BONUS", "5").strip())
MIN_DEPOSIT = float(os.getenv("MIN_DEPOSIT", "10").strip())

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
        {"$set": {"value": link, "updated_at": datetime.utcnow()}},
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
        {"$set": {"value": value, "updated_at": datetime.utcnow()}},
        upsert=True
    )

# ---------- LOGS CHANNEL HELPER ----------
async def log_event(text):
    if LOGS_CHANNEL_ID:
        try:
            await bot.send_message(LOGS_CHANNEL_ID, text)
        except Exception as e:
            logging.error(f"Failed to send log to channel: {e}")

# ---------- HELPER ----------
async def get_existing_countries():
    return await accounts_col.distinct("country", {})

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
            buttons.append([Button.url(f"📢 Join {title}", link)])
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
                buttons.append([Button.url(f"📢 Join {title}", invite_link)])
            else:
                buttons.append([Button.inline(f"🔒 {title} (join manually)", b"noop")])
    if not buttons:
        return
    buttons.append([Button.inline("✅ Check Again", b"check_join")])
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
        [Button.inline("🛒 Buy Account", b"buy"), Button.inline("💰 My Balance", b"balance")],
        [Button.inline("💳 Deposit", b"deposit"), Button.inline("📜 Order History", b"orders")],
    ]
    # Third row: Referral Program + Admin (if admin) - removed separate Withdrawal History button
    row3 = [Button.inline("👥 Referral Program", b"referral_info")]
    if user_id in ADMIN_IDS:
        row3.append(Button.inline("⚙️ Admin Panel", b"admin"))
    buttons.append(row3)

    support_link = await get_support_link()
    if support_link:
        buttons.append([Button.url("📞 Support", support_link)])

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
        [Button.inline("🛒 Buy Account", b"buy"), Button.inline("💰 My Balance", b"balance")],
        [Button.inline("💳 Deposit", b"deposit"), Button.inline("📜 Order History", b"orders")],
    ]
    row3 = [Button.inline("👥 Referral Program", b"referral_info")]
    if user_id in ADMIN_IDS:
        row3.append(Button.inline("⚙️ Admin Panel", b"admin"))
    buttons.append(row3)
    support_link = await get_support_link()
    if support_link:
        buttons.append([Button.url("📞 Support", support_link)])
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
        [Button.inline("✅ Confirm", b"broadcast_confirm")],
        [Button.inline("❌ Cancel", b"broadcast_cancel")],
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
            await log_event(f"📌 Broadcast pinned in logs channel by admin {user_id}")
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

# ---------- CALLBACK HANDLER ----------
@bot.on(events.CallbackQuery)
async def callback_handler(event):
    data = event.data.decode()
    user_id = event.sender_id

    if data.startswith("broadcast_"):
        return

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

    # Clear general states
    if data in ("main", "buy", "balance", "deposit", "orders", "admin",
                "admin_add_otp", "admin_add_sess", "admin_addbal",
                "admin_deposits", "admin_setprice", "admin_support", "withdraw",
                "admin_minwithdraw", "admin_transactions", "admin_accounts",
                "admin_withdrawals", "my_withdrawals"):
        user_states.pop(user_id, None)

    # ---------- ADMIN ACCOUNTS FILTER/PAGINATION ----------
    if data.startswith("admin_accounts_"):
        if user_id not in ADMIN_IDS:
            await event.answer("❌ Unauthorized", alert=True)
            return
        parts = data.split("_", 2)
        if len(parts) == 3:
            action = parts[1]
            value = parts[2]
        else:
            action = parts[1] if len(parts) > 1 else None
            value = None

        if action == "filter":
            if value in ("all", "available", "sold", "inactive"):
                user_states[user_id] = {
                    "admin_accounts_filter": value,
                    "admin_accounts_page": 1
                }
            else:
                await event.answer("Invalid filter", alert=True)
                return
            await show_accounts_list(event, user_id)
        elif action == "page":
            try:
                page = int(value)
                if page < 1:
                    page = 1
                state = user_states.get(user_id, {})
                state["admin_accounts_page"] = page
                user_states[user_id] = state
            except:
                await event.answer("Invalid page", alert=True)
                return
            await show_accounts_list(event, user_id)
        return

    # ---------- ADMIN TRANSACTIONS FILTER/PAGINATION ----------
    if data.startswith("admin_tx_"):
        if user_id not in ADMIN_IDS:
            await event.answer("❌ Unauthorized", alert=True)
            return
        parts = data.split("_", 2)
        if len(parts) == 3:
            action = parts[1]
            value = parts[2]
        else:
            action = parts[1] if len(parts) > 1 else None
            value = None

        if action == "filter":
            if value in ("all", "purchase", "deposit"):
                user_states[user_id] = {
                    "admin_tx_filter": value,
                    "admin_tx_page": 1
                }
            else:
                await event.answer("Invalid filter", alert=True)
                return
            await show_transactions_list(event, user_id)
        elif action == "page":
            try:
                page = int(value)
                if page < 1:
                    page = 1
                state = user_states.get(user_id, {})
                state["admin_tx_page"] = page
                user_states[user_id] = state
            except:
                await event.answer("Invalid page", alert=True)
                return
            await show_transactions_list(event, user_id)
        return

    # ---------- ADMIN WITHDRAWALS FILTER/PAGINATION ----------
    if data.startswith("admin_wd_"):
        if user_id not in ADMIN_IDS:
            await event.answer("❌ Unauthorized", alert=True)
            return
        parts = data.split("_", 2)
        if len(parts) == 3:
            action = parts[1]
            value = parts[2]
        else:
            action = parts[1] if len(parts) > 1 else None
            value = None

        if action == "filter":
            if value in ("all", "pending", "approved", "rejected"):
                user_states[user_id] = {
                    "admin_wd_filter": value,
                    "admin_wd_page": 1
                }
            else:
                await event.answer("Invalid filter", alert=True)
                return
            await show_admin_withdrawals(event, user_id)
        elif action == "page":
            try:
                page = int(value)
                if page < 1:
                    page = 1
                state = user_states.get(user_id, {})
                state["admin_wd_page"] = page
                user_states[user_id] = state
            except:
                await event.answer("Invalid page", alert=True)
                return
            await show_admin_withdrawals(event, user_id)
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
        return

    if data.startswith("logout_"):
        phone = data[len("logout_"):]
        await acc_mgr.logout_client(phone)
        await event.answer("🔒 Session terminated. You will no longer receive OTPs for this number.", alert=True)
        try:
            original_text = event.message.text if event.message else ""
            await event.edit(original_text + "\n\n🔒 *Session terminated.*", buttons=None)
        except:
            pass
        return

    # ---------- REFERRAL INFO (with Withdrawal History button) ----------
    if data == "referral_info":
        username = await get_bot_username()
        ref_link = f"https://t.me/{username}?start=ref{user_id}" if username else "N/A"
        invited_count = await users_col.count_documents({"referred_by": user_id})
        paid_count = await users_col.count_documents({"referred_by": user_id, "referral_bonus_paid": True})
        user_doc = await users_col.find_one({"user_id": user_id})
        withdrawable = user_doc.get('withdrawable_balance', 0) if user_doc else 0
        total_earned = paid_count * REFERRAL_BONUS

        text = (
            "👥 **Referral Program**\n\n"
            "🔗 **Your Referral Link:**\n"
            f"`{ref_link}`\n\n"
            f"💰 **Bonus:** ₹{REFERRAL_BONUS} per referral\n"
            f"(When your referred friend deposits ₹50 or more)\n\n"
            "📊 **Your Stats:**\n"
            f"• Total Invited: **{invited_count}** users\n"
            f"• Bonus Paid: **{paid_count}** users\n"
            f"• Total Earned: **₹{total_earned}**\n\n"
            f"💸 **Withdrawable Balance:** ₹{withdrawable}\n\n"
            "Share your link and start earning!"
        )
        buttons = [
            [Button.inline("💸 Withdraw", b"withdraw")],
            [Button.inline("📜 Withdrawal History", b"my_withdrawals")],
            [Button.inline("🔙 Back", b"main")]
        ]
        await event.edit(text, buttons=buttons)
        return

    # ---------- Withdraw flow start ----------
    if data == "withdraw":
        user_doc = await users_col.find_one({"user_id": user_id})
        withdrawable = user_doc.get('withdrawable_balance', 0) if user_doc else 0
        if withdrawable <= 0:
            await event.answer("❌ You have no withdrawable balance.", alert=True)
            return
        min_wd = await get_min_withdrawal()
        user_states[user_id] = {"action": "withdraw", "step": "amount"}
        await event.edit(
            f"💸 **Withdraw**\n\nYour withdrawable balance: ₹{withdrawable}\n"
            f"Minimum withdrawal: ₹{min_wd}\n"
            "Enter the amount you wish to withdraw (in ₹):",
            buttons=[[Button.inline("🔙 Cancel", b"referral_info")]]
        )
        return

    # ---------- USER WITHDRAWALS HISTORY (back to referral_info) ----------
    if data == "my_withdrawals":
        await show_user_withdrawals(event, user_id)
        return

    # ---------- User purchase flow ----------
    if data == "buy":
        countries = await accounts_col.distinct("country", {"status": "available"})
        if not countries:
            await event.answer("❌ No accounts available!", alert=True)
            return
        btns = [[Button.inline(c, f"country_{c}")] for c in countries]
        btns.append([Button.inline("🔙 Back", b"main")])
        await event.edit("🌍 Choose a country:", buttons=btns)

    elif data.startswith("country_"):
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
            btns.append([Button.inline(f"₹{price} ({count} available)", f"price_{country}_{price}")])
        btns.append([Button.inline("🔙 Back", b"buy")])
        await event.edit(
            f"🌍 Country: {country}\n📦 Total Stock: {total_count}\n💵 Select a price:",
            buttons=btns
        )

    elif data.startswith("price_"):
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
            [Button.inline("✅ Confirm Purchase", b"confirm_purchase")],
            [Button.inline("❌ Cancel", b"cancel_purchase")]
        ]
        await event.edit(confirm_text, buttons=buttons)

    elif data == "confirm_purchase":
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
                {"$set": {"status": "sold", "buyer_id": user_id, "sold_at": datetime.utcnow()}}
            )
            if updated is None:
                continue

            phone = acc["phone"]
            client = acc_mgr.clients.get(phone)
            valid = False
            error_msg = None

            if client:
                try:
                    await client.get_me()
                    valid = True
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
                                f"🔄 Status: Marked as `inactive` in DB.\n"
                                f"🕒 Time: {datetime.utcnow().strftime('%d/%m/%Y %H:%M')}"
                            )
                        except:
                            pass
                    await log_event(
                        f"⚠️ **Inactive Session**\n"
                        f"Phone: `{phone}`\n"
                        f"Country: {country}\n"
                        f"Price: ₹{price}\n"
                        f"Error: `{error_msg}`\n"
                        f"Status: Marked inactive"
                    )
                    continue
            else:
                await accounts_col.update_one({"_id": updated["_id"]}, {"$set": {"status": "inactive"}})
                for admin in ADMIN_IDS:
                    try:
                        await bot.send_message(admin,
                            f"⚠️ **Client Missing in Memory!**\n"
                            f"📱 Phone: `{phone}`\n"
                            f"🌍 Country: {country}\n"
                            f"💰 Price: ₹{price}\n"
                            f"❌ Error: Client not loaded / session string missing.\n"
                            f"🔄 Status: Marked as `inactive` in DB.\n"
                            f"🕒 Time: {datetime.utcnow().strftime('%d/%m/%Y %H:%M')}"
                        )
                    except:
                        pass
                await log_event(
                    f"⚠️ **Client Missing**\n"
                    f"Phone: `{phone}`\n"
                    f"Country: {country}\n"
                    f"Price: ₹{price}\n"
                    f"Status: Marked inactive (client not found)"
                )
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
        await users_col.update_one(
            {"user_id": user_id},
            {"$inc": {"balance": -price}, "$set": {"withdrawable_balance": new_withdrawable}},
            upsert=True
        )

        await orders_col.insert_one({
            "user_id": user_id,
            "account_id": str(acc["_id"]),
            "phone": phone,
            "country": country,
            "amount": price,
            "status": "completed",
            "created_at": datetime.utcnow()
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
                [Button.inline("🔄 Request New OTP", f"resend_{phone}")],
                [Button.inline("🔙 Main Menu", b"main")]
            ]
        )
        user_states.pop(user_id, None)

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
                    f"Balance After Purchase: ₹{new_balance}\n"
                    f"Date: {datetime.utcnow().strftime('%d/%m/%Y %H:%M')}"
                )
            except:
                pass

        await log_event(
            f"🛒 **Purchase**\n"
            f"Buyer: [{buyer_name}](tg://user?id={user_id}) (`{user_id}`)\n"
            f"Phone: `{phone}`\n"
            f"Country: {country}\n"
            f"Price: ₹{price}\n"
            f"Balance After: ₹{new_balance}\n"
            f"Date: {datetime.utcnow().strftime('%d/%m/%Y %H:%M')}"
        )

    elif data == "cancel_purchase":
        state = user_states.pop(user_id, None)
        if state and state.get("action") == "awaiting_confirmation":
            country = state["country"]
            total_count = await accounts_col.count_documents({"country": country, "status": "available"})
            if total_count == 0:
                await event.edit("❌ No accounts left in this country.", buttons=[[Button.inline("🔙 Back", b"buy")]])
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
                btns.append([Button.inline(f"₹{price_val} ({count} available)", f"price_{country}_{price_val}")])
            btns.append([Button.inline("🔙 Back", b"buy")])
            await event.edit(
                f"🌍 Country: {country}\n📦 Total Stock: {total_count}\n💵 Select a price:",
                buttons=btns
            )
        else:
            await event.edit("❌ Cancelled.", buttons=[[Button.inline("🔙 Main Menu", b"main")]])

    elif data.startswith("resend_"):
        phone = data.split("_", 1)[1]
        if phone not in acc_mgr.clients:
            await event.answer("❌ Session expired. Cannot receive OTP. Contact admin.", alert=True)
            return
        pending_otp_requests[(user_id, phone)] = True
        await event.answer("✅ Waiting for new OTP. Now try to log in again.", alert=True)
        async def clear_pending():
            await asyncio.sleep(90)
            key = (user_id, phone)
            if key in pending_otp_requests:
                del pending_otp_requests[key]
                try:
                    await bot.send_message(user_id, "⏰ No OTP received within 90 seconds. Please try again.")
                except:
                    pass
        asyncio.create_task(clear_pending())

    elif data == "balance":
        user = await users_col.find_one({"user_id": user_id})
        bal = user["balance"] if user else 0
        await event.edit(f"💰 Your balance: ₹{bal}", buttons=[[Button.inline("🔙 Back", b"main")]])

    elif data == "deposit":
        user_states[user_id] = {"action": "deposit", "step": "amount"}
        await event.edit(
            f"💵 Enter the amount you want to deposit (₹) – Minimum deposit is ₹{MIN_DEPOSIT}:",
            buttons=[[Button.inline("🔙 Cancel", b"main")]]
        )

    elif data == "orders":
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
                "status": "Completed"
            })
        for d in deposits:
            combined.append({
                "type": "Deposit",
                "phone": "N/A",
                "country": "N/A",
                "amount": d.get("amount", 0),
                "date": d["created_at"],
                "status": "Approved"
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

        await event.edit(txt, buttons=[[Button.inline("🔙 Back", b"main")]])

    elif data == "main":
        await send_main_menu(event)

    # ---------- ADMIN PANEL ----------
    elif data == "admin":
        if user_id not in ADMIN_IDS:
            await event.answer("❌ Unauthorized", alert=True)
            return
        btns = [
            [Button.inline("➕ Add Account (OTP)", b"admin_add_otp")],
            [Button.inline("📥 Add Account (Session)", b"admin_add_sess")],
            [Button.inline("📋 Accounts (Filter)", b"admin_accounts_filter_all")],
            [Button.inline("💰 Add Balance", b"admin_addbal")],
            [Button.inline("💲 Set Price", b"admin_setprice")],
            [Button.inline("🕒 Pending Deposits", b"admin_deposits")],
            [Button.inline("📜 Transaction History", b"admin_tx_filter_all")],
            [Button.inline("💸 Withdrawal History", b"admin_wd_filter_all")],
            [Button.inline("📞 Set Support Link", b"admin_support")],
            [Button.inline("💸 Set Min Withdrawal", b"admin_minwithdraw")],
            [Button.inline("🔙 Back", b"main")],
        ]
        await event.edit("⚙️ **Admin Panel**", buttons=btns)

    elif data == "admin_add_otp":
        await start_add_phone_flow(event)
    elif data == "admin_add_sess":
        await start_add_session_flow(event)

    elif data == "admin_addbal":
        user_states[user_id] = {"action": "add_balance", "step": "await_user_id"}
        await event.edit("👤 Send the user ID:", buttons=[[Button.inline("🔙 Cancel", b"admin")]])

    elif data == "admin_deposits":
        cursor = deposits_col.find({"status": "pending"}).sort("created_at", 1)
        pending = await cursor.to_list(length=10)
        if not pending:
            await event.answer("No pending deposits.", alert=True)
            return
        btns = []
        for dep in pending:
            txn_id = dep.get('txn_id', 'N/A')
            btns.append([
                Button.inline(f"✅ Approve ₹{dep['amount']} ({txn_id})", f"approve_{dep['_id']}"),
                Button.inline(f"❌ Reject", f"reject_{dep['_id']}")
            ])
        btns.append([Button.inline("🔙 Back", b"admin")])
        await event.edit("🕒 **Pending Deposits**", buttons=btns)

    elif data.startswith("approve_"):
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

        bonus_paid = False
        user_doc = await users_col.find_one({"user_id": user_id_dep})
        if user_doc and user_doc.get("referred_by"):
            if not user_doc.get("referral_bonus_paid"):
                total_dep = await deposits_col.aggregate([
                    {"$match": {"user_id": user_id_dep, "status": "approved"}},
                    {"$group": {"_id": None, "total": {"$sum": "$amount"}}}
                ]).to_list(length=1)
                total = total_dep[0]["total"] if total_dep else 0
                if total >= 50:
                    referrer_id = user_doc["referred_by"]
                    await users_col.update_one(
                        {"user_id": referrer_id},
                        {"$inc": {"balance": REFERRAL_BONUS, "withdrawable_balance": REFERRAL_BONUS}}
                    )
                    await users_col.update_one(
                        {"user_id": user_id_dep},
                        {"$set": {"referral_bonus_paid": True}}
                    )
                    bonus_paid = True
                    try:
                        await bot.send_message(referrer_id,
                            f"🎉 Your referral {user_id_dep} has deposited ₹{total}.\n"
                            f"You earned ₹{REFERRAL_BONUS} referral bonus!")
                    except:
                        pass
                    await log_event(
                        f"🎁 **Referral Bonus**\n"
                        f"Referrer: [{referrer_id}](tg://user?id={referrer_id})\n"
                        f"Referred User: [{user_id_dep}](tg://user?id={user_id_dep})\n"
                        f"Total Deposits: ₹{total}\n"
                        f"Bonus: ₹{REFERRAL_BONUS}"
                    )

        try:
            await bot.send_message(user_id_dep,
                                   f"✅ Deposit of ₹{amount} approved! Balance updated.")
        except:
            pass

        await event.edit("✅ Deposit approved!", buttons=[[Button.inline("🔙 Admin Menu", b"admin")]])
        await log_event(
            f"✅ **Deposit Approved**\n"
            f"User: [{user_id_dep}](tg://user?id={user_id_dep})\n"
            f"Amount: ₹{amount}\n"
            f"Txn ID: {deposit.get('txn_id', 'N/A')}\n"
            f"Referral Bonus: {'Yes' if bonus_paid else 'No'}\n"
            f"Date: {datetime.utcnow().strftime('%d/%m/%Y %H:%M')}"
        )

    elif data.startswith("reject_"):
        dep_id = data.split("_", 1)[1]
        deposit = await deposits_col.find_one({"_id": ObjectId(dep_id)})
        if not deposit or deposit["status"] != "pending":
            await event.answer("Already processed.", alert=True)
            return
        await deposits_col.update_one({"_id": ObjectId(dep_id)}, {"$set": {"status": "rejected"}})
        try:
            await bot.send_message(deposit["user_id"],
                                   f"❌ Deposit of ₹{deposit['amount']} rejected. Contact admin.")
        except:
            pass
        await event.edit("❌ Deposit rejected.", buttons=[[Button.inline("🔙 Admin Menu", b"admin")]])

    elif data == "admin_setprice":
        if user_id not in ADMIN_IDS:
            await event.answer("❌ Unauthorized", alert=True)
            return
        user_states[user_id] = {"action": "set_price", "step": "await_price"}
        await event.edit(
            "💲 **Set Default Price**\n\n"
            "Send the new default price for accounts (e.g., `50`).\n"
            "This price will apply when adding new accounts if not specified.",
            buttons=[[Button.inline("🔙 Cancel", b"admin")]]
        )

    elif data == "admin_support":
        if user_id not in ADMIN_IDS:
            await event.answer("❌ Unauthorized", alert=True)
            return
        current = await get_support_link()
        current_display = f"`{current}`" if current else "*Not set*"
        user_states[user_id] = {"action": "set_support_link", "step": "await_link"}
        await event.edit(
            f"📞 **Set Support Link**\n\n"
            f"Current: {current_display}\n\n"
            f"Send the new support link (e.g., `https://t.me/your_support_username` or `t.me/...`).\n"
            f"Send `remove` to delete the support link.",
            buttons=[[Button.inline("🔙 Cancel", b"admin")]]
        )

    elif data == "admin_minwithdraw":
        if user_id not in ADMIN_IDS:
            await event.answer("❌ Unauthorized", alert=True)
            return
        current = await get_min_withdrawal()
        user_states[user_id] = {"action": "set_min_withdraw", "step": "await_value"}
        await event.edit(
            f"💸 **Set Minimum Withdrawal Amount**\n\n"
            f"Current minimum: ₹{current}\n\n"
            "Send the new minimum withdrawal amount (e.g., `20`):",
            buttons=[[Button.inline("🔙 Cancel", b"admin")]]
        )

    elif data.startswith("addcountry_"):
        if data == "addcountry_new":
            state = user_states.get(user_id)
            if not state or state.get("action") not in ("add_phone_otp", "add_session"):
                await event.answer("❌ Session expired. Please start again from Admin Panel.", alert=True)
                return
            state["step"] = "country_manual"
            await event.edit("🌍 Send the new country code (e.g., IN):",
                             buttons=[[Button.inline("🔙 Cancel", b"admin")]])
        else:
            country = data[len("addcountry_"):]
            state = user_states.get(user_id)
            if not state or state.get("action") not in ("add_phone_otp", "add_session"):
                await event.answer("❌ Session expired. Please start again.", alert=True)
                return
            state["country"] = country
            state["step"] = "price"
            await event.edit("💵 Send price for this number (e.g., 50):",
                             buttons=[[Button.inline("🔙 Cancel", b"admin")]])

    elif data.startswith("wapprove_") or data.startswith("wreject_"):
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
            user_doc = await users_col.find_one({"user_id": withdrawal["user_id"]})
            if not user_doc:
                await event.answer("User not found.", alert=True)
                return
            if user_doc.get("withdrawable_balance", 0) < withdrawal["amount"]:
                await event.answer("User doesn't have sufficient withdrawable balance now.", alert=True)
                return
            await users_col.update_one(
                {"user_id": withdrawal["user_id"]},
                {"$inc": {"balance": -withdrawal["amount"], "withdrawable_balance": -withdrawal["amount"]}}
            )
            await withdrawals_col.update_one(
                {"_id": ObjectId(w_id)},
                {"$set": {"status": "approved", "processed_at": datetime.utcnow()}}
            )
            try:
                await bot.send_message(
                    withdrawal["user_id"],
                    f"✅ Your withdrawal of ₹{withdrawal['amount']} has been approved and processed."
                )
            except:
                pass
            await event.edit("✅ Withdrawal approved.", buttons=[[Button.inline("🔙 Admin Menu", b"admin")]])
            await log_event(
                f"✅ **Withdrawal Approved**\n"
                f"User: [{withdrawal['user_id']}](tg://user?id={withdrawal['user_id']})\n"
                f"Amount: ₹{withdrawal['amount']}\n"
                f"UPI: {withdrawal.get('upi_id', 'N/A')}\n"
                f"Date: {datetime.utcnow().strftime('%d/%m/%Y %H:%M')}"
            )
        else:  # wreject
            await withdrawals_col.update_one(
                {"_id": ObjectId(w_id)},
                {"$set": {"status": "rejected", "processed_at": datetime.utcnow()}}
            )
            try:
                await bot.send_message(
                    withdrawal["user_id"],
                    f"❌ Your withdrawal of ₹{withdrawal['amount']} has been rejected. Contact admin."
                )
            except:
                pass
            await event.edit("❌ Withdrawal rejected.", buttons=[[Button.inline("🔙 Admin Menu", b"admin")]])
            await log_event(
                f"❌ **Withdrawal Rejected**\n"
                f"User: [{withdrawal['user_id']}](tg://user?id={withdrawal['user_id']})\n"
                f"Amount: ₹{withdrawal['amount']}\n"
                f"Date: {datetime.utcnow().strftime('%d/%m/%Y %H:%M')}"
            )

    else:
        await event.answer("Unknown action", alert=True)

# ---------- ADMIN ACCOUNTS LIST ----------
async def show_accounts_list(event, user_id):
    state = user_states.get(user_id, {})
    filter_status = state.get("admin_accounts_filter", "all")
    page = state.get("admin_accounts_page", 1)
    per_page = 10

    query = {}
    if filter_status != "all":
        query["status"] = filter_status

    total = await accounts_col.count_documents(query)
    total_pages = (total + per_page - 1) // per_page if total > 0 else 1
    if page > total_pages:
        page = total_pages
        state["admin_accounts_page"] = page
        user_states[user_id] = state

    skip = (page - 1) * per_page
    cursor = accounts_col.find(query).sort("_id", -1).skip(skip).limit(per_page)
    accounts = await cursor.to_list(length=per_page)

    if not accounts:
        txt = f"📋 **Accounts ({filter_status})**\n\nNo accounts found."
    else:
        lines = []
        for acc in accounts:
            status_emoji = {"available": "🟢", "sold": "🔴", "inactive": "⚪"}.get(acc.get("status"), "❓")
            buyer = f" (buyer:{acc['buyer_id']})" if acc.get("buyer_id") else ""
            lines.append(
                f"{status_emoji} `{acc['phone']}` | {acc['country']} | ₹{acc.get('price', '?')} | {acc.get('status', 'unknown')}{buyer}"
            )
        txt = f"📋 **Accounts ({filter_status})** – Page {page}/{total_pages}\n" + "\n".join(lines)

    btns = []
    filter_row = [
        Button.inline("🟢 All", f"admin_accounts_filter_all"),
        Button.inline("🟢 Available", f"admin_accounts_filter_available"),
        Button.inline("🔴 Sold", f"admin_accounts_filter_sold"),
        Button.inline("⚪ Inactive", f"admin_accounts_filter_inactive")
    ]
    btns.append(filter_row)

    page_row = []
    if page > 1:
        page_row.append(Button.inline("⬅️ Prev", f"admin_accounts_page_{page-1}"))
    if page < total_pages:
        page_row.append(Button.inline("Next ➡️", f"admin_accounts_page_{page+1}"))
    if page_row:
        btns.append(page_row)

    btns.append([Button.inline("🔙 Back", b"admin")])

    if isinstance(event, events.CallbackQuery.Event):
        await event.edit(txt, buttons=btns)
    else:
        await event.respond(txt, buttons=btns)

# ---------- ADMIN TRANSACTION HISTORY ----------
async def show_transactions_list(event, user_id):
    state = user_states.get(user_id, {})
    filter_type = state.get("admin_tx_filter", "all")
    page = state.get("admin_tx_page", 1)
    per_page = 10

    orders_cursor = orders_col.find({}).sort("created_at", -1)
    deposits_cursor = deposits_col.find({"status": "approved"}).sort("created_at", -1)
    orders = await orders_cursor.to_list(length=None)
    deposits = await deposits_cursor.to_list(length=None)

    combined = []
    for o in orders:
        combined.append({
            "type": "purchase",
            "user_id": o["user_id"],
            "phone": o.get("phone", "N/A"),
            "country": o.get("country", "N/A"),
            "amount": o.get("amount", 0),
            "date": o["created_at"],
            "status": "Completed"
        })
    for d in deposits:
        combined.append({
            "type": "deposit",
            "user_id": d["user_id"],
            "phone": "N/A",
            "country": "N/A",
            "amount": d.get("amount", 0),
            "date": d["created_at"],
            "txn_id": d.get("txn_id", "N/A"),
            "status": "Approved"
        })

    if filter_type == "purchase":
        combined = [x for x in combined if x["type"] == "purchase"]
    elif filter_type == "deposit":
        combined = [x for x in combined if x["type"] == "deposit"]

    combined.sort(key=lambda x: x["date"], reverse=True)

    total = len(combined)
    total_pages = (total + per_page - 1) // per_page if total > 0 else 1
    if page > total_pages:
        page = total_pages
        state["admin_tx_page"] = page
        user_states[user_id] = state

    start = (page - 1) * per_page
    end = min(start + per_page, total)
    page_items = combined[start:end]

    if not page_items:
        txt = f"📜 **Transaction History ({filter_type})**\n\nNo transactions found."
    else:
        lines = []
        for item in page_items:
            date_str = item["date"].strftime('%d/%m/%Y %H:%M')
            if item["type"] == "purchase":
                lines.append(
                    f"🛒 User {item['user_id']} | {item['phone']} ({item['country']}) | -₹{item['amount']} | {date_str}"
                )
            else:
                lines.append(
                    f"💰 User {item['user_id']} | Deposit | +₹{item['amount']} | Txn:{item.get('txn_id','N/A')} | {date_str}"
                )
        txt = f"📜 **Transaction History ({filter_type})** – Page {page}/{total_pages}\n" + "\n".join(lines)

    btns = []
    filter_row = [
        Button.inline("📋 All", f"admin_tx_filter_all"),
        Button.inline("🛒 Purchases", f"admin_tx_filter_purchase"),
        Button.inline("💰 Deposits", f"admin_tx_filter_deposit")
    ]
    btns.append(filter_row)

    page_row = []
    if page > 1:
        page_row.append(Button.inline("⬅️ Prev", f"admin_tx_page_{page-1}"))
    if page < total_pages:
        page_row.append(Button.inline("Next ➡️", f"admin_tx_page_{page+1}"))
    if page_row:
        btns.append(page_row)

    btns.append([Button.inline("🔙 Back", b"admin")])

    if isinstance(event, events.CallbackQuery.Event):
        await event.edit(txt, buttons=btns)
    else:
        await event.respond(txt, buttons=btns)

# ---------- ADMIN WITHDRAWALS HISTORY ----------
async def show_admin_withdrawals(event, user_id):
    state = user_states.get(user_id, {})
    filter_status = state.get("admin_wd_filter", "all")
    page = state.get("admin_wd_page", 1)
    per_page = 10

    query = {}
    if filter_status != "all":
        query["status"] = filter_status

    total = await withdrawals_col.count_documents(query)
    total_pages = (total + per_page - 1) // per_page if total > 0 else 1
    if page > total_pages:
        page = total_pages
        state["admin_wd_page"] = page
        user_states[user_id] = state

    skip = (page - 1) * per_page
    cursor = withdrawals_col.find(query).sort("created_at", -1).skip(skip).limit(per_page)
    withdrawals = await cursor.to_list(length=per_page)

    if not withdrawals:
        txt = f"💸 **Withdrawal History ({filter_status})**\n\nNo withdrawals found."
    else:
        lines = []
        for wd in withdrawals:
            status_emoji = {"pending": "🟡", "approved": "🟢", "rejected": "🔴"}.get(wd.get("status"), "❓")
            date_str = wd["created_at"].strftime('%d/%m/%Y %H:%M')
            lines.append(
                f"{status_emoji} User {wd['user_id']} | ₹{wd['amount']} | UPI: {wd.get('upi_id','N/A')} | {wd.get('status','unknown')} | {date_str}"
            )
        txt = f"💸 **Withdrawal History ({filter_status})** – Page {page}/{total_pages}\n" + "\n".join(lines)

    btns = []
    filter_row = [
        Button.inline("📋 All", f"admin_wd_filter_all"),
        Button.inline("🟡 Pending", f"admin_wd_filter_pending"),
        Button.inline("🟢 Approved", f"admin_wd_filter_approved"),
        Button.inline("🔴 Rejected", f"admin_wd_filter_rejected")
    ]
    btns.append(filter_row)

    page_row = []
    if page > 1:
        page_row.append(Button.inline("⬅️ Prev", f"admin_wd_page_{page-1}"))
    if page < total_pages:
        page_row.append(Button.inline("Next ➡️", f"admin_wd_page_{page+1}"))
    if page_row:
        btns.append(page_row)

    btns.append([Button.inline("🔙 Back", b"admin")])

    if isinstance(event, events.CallbackQuery.Event):
        await event.edit(txt, buttons=btns)
    else:
        await event.respond(txt, buttons=btns)

# ---------- USER WITHDRAWALS HISTORY (with back to referral) ----------
async def show_user_withdrawals(event, user_id):
    state = user_states.get(user_id, {})
    page = state.get("user_wd_page", 1)
    per_page = 10

    query = {"user_id": user_id}
    total = await withdrawals_col.count_documents(query)
    total_pages = (total + per_page - 1) // per_page if total > 0 else 1
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
        page_row.append(Button.inline("⬅️ Prev", f"user_wd_page_{page-1}"))
    if page < total_pages:
        page_row.append(Button.inline("Next ➡️", f"user_wd_page_{page+1}"))
    if page_row:
        btns.append(page_row)

    # Back button goes to Referral Info instead of main menu
    btns.append([Button.inline("🔙 Back to Referral", b"referral_info")])

    if isinstance(event, events.CallbackQuery.Event):
        await event.edit(txt, buttons=btns)
    else:
        await event.respond(txt, buttons=btns)

# ---------- ADD PHONE (OTP) FLOW ----------
async def start_add_phone_flow(event):
    user_states[event.sender_id] = {"action": "add_phone_otp", "step": "phone"}
    await event.edit("📱 Send the phone number in international format (e.g., +919876543210):",
                     buttons=[[Button.inline("🔙 Cancel", b"admin")]])

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
            await event.respond("✉️ OTP sent! Send the code:",
                                buttons=[[Button.inline("🔙 Cancel", b"admin")]])
        except Exception as e:
            await temp_client.disconnect()
            await event.respond(f"❌ Error: {str(e)}", buttons=[[Button.inline("🔙 Cancel", b"admin")]])
            user_states.pop(user_id, None)
    elif step == "otp":
        code = event.message.text.strip()
        temp_client = state["temp_client"]
        try:
            await temp_client.sign_in(state["phone"], code)
        except SessionPasswordNeededError:
            state["step"] = "2fa"
            await event.respond("🔒 2FA password required. Send password:",
                                buttons=[[Button.inline("🔙 Cancel", b"admin")]])
            return
        except Exception as e:
            await temp_client.disconnect()
            await event.respond(f"❌ Login failed: {str(e)}", buttons=[[Button.inline("🔙 Cancel", b"admin")]])
            user_states.pop(user_id, None)
            return
        session_str = temp_client.session.save()
        state["session"] = session_str
        state["step"] = "choose_country"
        existing = await get_existing_countries()
        btns = [[Button.inline(c, f"addcountry_{c}")] for c in existing]
        btns.append([Button.inline("➕ New Country", b"addcountry_new")])
        btns.append([Button.inline("🔙 Cancel", b"admin")])
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
            btns = [[Button.inline(c, f"addcountry_{c}")] for c in existing]
            btns.append([Button.inline("➕ New Country", b"addcountry_new")])
            btns.append([Button.inline("🔙 Cancel", b"admin")])
            await temp_client.disconnect()
            await event.respond("🌍 Select country or add new:", buttons=btns)
        except Exception as e:
            await temp_client.disconnect()
            await event.respond(f"❌ 2FA failed: {str(e)}", buttons=[[Button.inline("🔙 Cancel", b"admin")]])
            user_states.pop(user_id, None)
    elif step == "country_manual":
        country = event.message.text.strip().upper()
        state["country"] = country
        state["step"] = "price"
        await event.respond("💵 Send price for this number (e.g., 50):",
                            buttons=[[Button.inline("🔙 Cancel", b"admin")]])
    elif step == "price":
        try:
            price = float(event.message.text.strip())
            if price <= 0:
                raise ValueError
        except:
            await event.respond("❌ Invalid price. Send a positive number:",
                                buttons=[[Button.inline("🔙 Cancel", b"admin")]])
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
        await event.respond(f"✅ Account `{phone}` ({country}) added at ₹{price}!",
                            buttons=[[Button.inline("🔙 Admin Menu", b"admin")]])
        user_states.pop(user_id, None)

# ---------- ADD SESSION FLOW ----------
async def start_add_session_flow(event):
    user_states[event.sender_id] = {"action": "add_session", "step": "session"}
    await event.edit("🔑 Send the session string:",
                     buttons=[[Button.inline("🔙 Cancel", b"admin")]])

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
                await event.respond(
                    "❌ Session authorized nahi hai. Kya aapne incomplete session diya hai?\n"
                    "Is account ko add karne ke liye 'Add Account (OTP)' use karein.",
                    buttons=[[Button.inline("🔙 Admin Menu", b"admin")]]
                )
                user_states.pop(user_id, None)
                return
            me = await temp_client.get_me()
            phone = me.phone
            state["phone"] = phone
            state["client"] = temp_client
            state["step"] = "ask_2fa"
            await event.respond(
                f"📱 Number: {phone}\n\n"
                "🔐 Kya is account ka koi 2FA password hai?\n"
                "Password bhejo, ya 'skip' type karo.",
                buttons=[[Button.inline("🔙 Cancel", b"admin")]]
            )
        except Exception as e:
            await temp_client.disconnect()
            await event.respond(f"❌ Error: {str(e)}", buttons=[[Button.inline("🔙 Cancel", b"admin")]])
            user_states.pop(user_id, None)
    elif step == "ask_2fa":
        answer = event.message.text.strip()
        if answer.lower() != "skip":
            state["twofa_password"] = answer
        state["step"] = "choose_country"
        existing = await get_existing_countries()
        btns = [[Button.inline(c, f"addcountry_{c}")] for c in existing]
        btns.append([Button.inline("➕ New Country", b"addcountry_new")])
        btns.append([Button.inline("🔙 Cancel", b"admin")])
        await event.respond("🌍 Select country or add new:", buttons=btns)
    elif step == "country_manual":
        country = event.message.text.strip().upper()
        state["country"] = country
        state["step"] = "price"
        await event.respond("💵 Send price for this number (e.g., 50):",
                            buttons=[[Button.inline("🔙 Cancel", b"admin")]])
    elif step == "price":
        try:
            price = float(event.message.text.strip())
            if price <= 0:
                raise ValueError
        except:
            await event.respond("❌ Invalid price. Send a positive number:",
                                buttons=[[Button.inline("🔙 Cancel", b"admin")]])
            return
        phone = state["phone"]
        country = state["country"]
        session_str = state["session_str"]
        client = state["client"]
        new_session = client.session.save()
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
        await client.disconnect()
        await event.respond(f"✅ Account `{phone}` ({country}) added at ₹{price}!",
                            buttons=[[Button.inline("🔙 Admin Menu", b"admin")]])
        user_states.pop(user_id, None)

# ---------- DEPOSIT FLOW ----------
async def process_deposit_step(event):
    user_id = event.sender_id
    state = user_states.get(user_id)
    if not state or state["action"] != "deposit":
        return
    step = state["step"]
    if step == "amount":
        try:
            amount = float(event.message.text.strip())
            if amount <= 0:
                raise ValueError
            if amount < MIN_DEPOSIT:
                await event.respond(
                    f"❌ Minimum deposit is ₹{MIN_DEPOSIT}. Please enter a valid amount:",
                    buttons=[[Button.inline("🔙 Cancel", b"main")]]
                )
                return
        except:
            await event.respond(
                f"❌ Invalid amount. Minimum is ₹{MIN_DEPOSIT}. Enter again:",
                buttons=[[Button.inline("🔙 Cancel", b"main")]]
            )
            return
        state["amount"] = amount

        txn_id = f"DEP{datetime.now().strftime('%y%m%d%H%M')}{random.randint(1000,9999)}"
        state["txn_id"] = txn_id

        upi_string = f"upi://pay?pa={UPI_ID}&pn={PAYEE_NAME}&am={amount}&tn=OTP_Deposit"
        img = qrcode.make(upi_string)
        buf = io.BytesIO()
        img.save(buf, format='PNG')
        buf.seek(0)
        buf.name = "qr_code.png"

        caption = (
            f"💳 **Deposit ₹{amount}**\n"
            f"🔑 **Txn ID:** `{txn_id}`\n\n"
            f"📌 **Please mention this Txn ID in your payment note.**\n\n"
            f"Scan QR or use UPI ID: `{UPI_ID}`\n\n"
            "Payment karne ke baad uska **screenshot yahan bhejo**."
        )
        await bot.send_file(
            event.chat_id,
            buf,
            caption=caption,
            buttons=[[Button.inline("🔙 Cancel", b"main")]]
        )
        state["step"] = "screenshot"
    elif step == "screenshot":
        if not event.message.photo:
            await event.respond("❌ Kripya payment ka screenshot bhejein, text nahi.",
                                buttons=[[Button.inline("🔙 Cancel", b"main")]])
            return
        amount = state["amount"]
        txn_id = state.get("txn_id", "N/A")
        result = await deposits_col.insert_one({
            "user_id": user_id,
            "amount": amount,
            "txn_id": txn_id,
            "proof_type": "screenshot",
            "status": "pending",
            "created_at": datetime.utcnow()
        })
        dep_id = result.inserted_id
        photo_bytes = await event.message.download_media(file=bytes)
        photo_io = io.BytesIO(photo_bytes)
        photo_io.name = "payment_proof.jpg"
        for admin in ADMIN_IDS:
            try:
                await bot.send_file(admin,
                    photo_io,
                    caption=f"🔔 **New Deposit Request**\nUser: `{user_id}`\nAmount: ₹{amount}\nTxn ID: `{txn_id}`\nProof: Screenshot",
                    buttons=[
                        [Button.inline("✅ Approve", f"approve_{dep_id}"),
                         Button.inline("❌ Reject", f"reject_{dep_id}")]
                    ])
                photo_io.seek(0)
            except:
                pass
        await event.respond(
            f"✅ Deposit request submitted!\nAmount: ₹{amount}\nTxn ID: `{txn_id}`\nAdmin will verify your screenshot and approve.",
            buttons=[[Button.inline("🔙 Main Menu", b"main")]]
        )
        user_states.pop(user_id, None)

        await log_event(
            f"💳 **Deposit Request**\n"
            f"User: [{user_id}](tg://user?id={user_id})\n"
            f"Amount: ₹{amount}\n"
            f"Txn ID: `{txn_id}`\n"
            f"Date: {datetime.utcnow().strftime('%d/%m/%Y %H:%M')}"
        )

# ---------- HANDLE ALL TEXT MESSAGES ----------
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
                await event.respond("❌ Invalid user ID. Send a numeric ID:",
                                    buttons=[[Button.inline("🔙 Cancel", b"admin")]])
                return
            state["uid"] = uid
            state["step"] = "await_amount"
            await event.respond("💵 Send amount to add:",
                                buttons=[[Button.inline("🔙 Cancel", b"admin")]])
        elif step == "await_amount":
            try:
                amt = float(event.message.text.strip())
            except:
                await event.respond("❌ Invalid amount. Try again:",
                                    buttons=[[Button.inline("🔙 Cancel", b"admin")]])
                return
            uid = state["uid"]
            await users_col.update_one(
                {"user_id": uid},
                {"$inc": {"balance": amt}, "$setOnInsert": {"joined_at": datetime.utcnow()}},
                upsert=True
            )
            await event.respond(f"✅ Added ₹{amt} to user `{uid}`.",
                                buttons=[[Button.inline("🔙 Admin Menu", b"admin")]])
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
                await event.respond(
                    "❌ Invalid amount. Enter a positive number:",
                    buttons=[[Button.inline("🔙 Cancel", b"referral_info")]]
                )
                return
            user_doc = await users_col.find_one({"user_id": user_id})
            withdrawable = user_doc.get('withdrawable_balance', 0) if user_doc else 0
            if amount > withdrawable:
                await event.respond(
                    f"❌ You have only ₹{withdrawable} withdrawable balance. Enter a lower amount:",
                    buttons=[[Button.inline("🔙 Cancel", b"referral_info")]]
                )
                return
            min_wd = await get_min_withdrawal()
            if amount < min_wd:
                await event.respond(
                    f"❌ Minimum withdrawal is ₹{min_wd}. Please enter a higher amount:",
                    buttons=[[Button.inline("🔙 Cancel", b"referral_info")]]
                )
                return
            state["amount"] = amount
            state["step"] = "upi"
            await event.respond(
                "💳 Enter your UPI ID (e.g., `example@upi`):",
                buttons=[[Button.inline("🔙 Cancel", b"referral_info")]]
            )
        elif step == "upi":
            upi = event.message.text.strip()
            if not upi or "@" not in upi:
                await event.respond(
                    "❌ Invalid UPI ID. Please enter a valid one (e.g., `example@upi`):",
                    buttons=[[Button.inline("🔙 Cancel", b"referral_info")]]
                )
                return
            amount = state["amount"]
            result = await withdrawals_col.insert_one({
                "user_id": user_id,
                "amount": amount,
                "upi_id": upi,
                "status": "pending",
                "created_at": datetime.utcnow()
            })
            w_id = result.inserted_id
            for admin in ADMIN_IDS:
                try:
                    await bot.send_message(
                        admin,
                        f"🔔 **New Withdrawal Request**\n"
                        f"User: `{user_id}`\n"
                        f"Amount: ₹{amount}\n"
                        f"UPI: `{upi}`\n"
                        f"Date: {datetime.utcnow().strftime('%d/%m/%Y %H:%M')}",
                        buttons=[
                            [Button.inline("✅ Approve", f"wapprove_{w_id}"),
                             Button.inline("❌ Reject", f"wreject_{w_id}")]
                        ]
                    )
                except:
                    pass
            await event.respond(
                f"✅ Withdrawal request of ₹{amount} submitted to UPI `{upi}`.\n"
                "Admin will process it shortly.",
                buttons=[[Button.inline("🔙 Referral Info", b"referral_info")]]
            )
            await log_event(
                f"💸 **Withdrawal Request**\n"
                f"User: [{user_id}](tg://user?id={user_id})\n"
                f"Amount: ₹{amount}\n"
                f"UPI: `{upi}`\n"
                f"Date: {datetime.utcnow().strftime('%d/%m/%Y %H:%M')}"
            )
            user_states.pop(user_id, None)
    elif action == "set_support_link":
        step = state.get("step")
        if step == "await_link":
            link = event.message.text.strip()
            if link.lower() == "remove":
                await settings_col.delete_one({"key": "support_link"})
                await event.respond("✅ Support link removed.",
                                    buttons=[[Button.inline("🔙 Admin Menu", b"admin")]])
            else:
                if not (link.startswith("http://") or link.startswith("https://") or link.startswith("t.me/")):
                    await event.respond(
                        "❌ Invalid link. Please send a valid URL starting with `http://`, `https://`, or `t.me/`.\nTry again:",
                        buttons=[[Button.inline("🔙 Cancel", b"admin")]]
                    )
                    return
                await set_support_link(link)
                await event.respond(f"✅ Support link updated to:\n`{link}`",
                                    buttons=[[Button.inline("🔙 Admin Menu", b"admin")]])
            user_states.pop(user_id, None)
    elif action == "set_price":
        step = state.get("step")
        if step == "await_price":
            try:
                new_price = float(event.message.text.strip())
                if new_price <= 0:
                    raise ValueError
            except:
                await event.respond("❌ Invalid price. Send a positive number (e.g., 50):",
                                    buttons=[[Button.inline("🔙 Cancel", b"admin")]])
                return
            await settings_col.update_one(
                {"key": "default_price"},
                {"$set": {"value": new_price, "updated_at": datetime.utcnow()}},
                upsert=True
            )
            global DEFAULT_PRICE
            DEFAULT_PRICE = new_price
            await event.respond(f"✅ Default price updated to ₹{new_price}.",
                                buttons=[[Button.inline("🔙 Admin Menu", b"admin")]])
            user_states.pop(user_id, None)
    elif action == "set_min_withdraw":
        step = state.get("step")
        if step == "await_value":
            try:
                val = float(event.message.text.strip())
                if val <= 0:
                    raise ValueError
            except:
                await event.respond("❌ Invalid value. Enter a positive number (e.g., `20`):",
                                    buttons=[[Button.inline("🔙 Cancel", b"admin")]])
                return
            await set_min_withdrawal(val)
            await event.respond(f"✅ Minimum withdrawal updated to ₹{val}.",
                                buttons=[[Button.inline("🔙 Admin Menu", b"admin")]])
            user_states.pop(user_id, None)
    else:
        await send_main_menu(event)

# ---------- /start COMMAND ----------
@bot.on(events.NewMessage(pattern='/start'))
async def start_cmd(event):
    user_id = event.sender_id
    args = event.message.text.split()
    referrer_id = None

    if len(args) > 1 and args[1].startswith('ref'):
        try:
            referrer_id = int(args[1][3:])
        except:
            referrer_id = None

    user_data = await users_col.find_one({"user_id": user_id})
    if not user_data:
        await users_col.insert_one({
            "user_id": user_id,
            "balance": 0,
            "joined_at": datetime.utcnow(),
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

# ---------- MAIN FUNCTION ----------
async def main():
    if not BOT_TOKEN:
        logging.error("❌ BOT_TOKEN is empty or missing in .env file!")
        return

    try:
        await bot.start(bot_token=BOT_TOKEN)
    except AccessTokenInvalidError:
        logging.error("❌ Invalid BOT_TOKEN! Please check your .env file.")
        return
    except Exception as e:
        error_msg = str(e)
        if "database is locked" in error_msg or "unable to open database file" in error_msg:
            logging.error("❌ Database (session file) locked! Probably another bot instance is running.")
            logging.error("👉 Run: pkill -f bot.py   and then start again.")
        else:
            logging.error(f"❌ Failed to start bot: {e}")
        return

    global acc_mgr
    acc_mgr = AccountManager(accounts_col, bot, API_ID, API_HASH, pending_otp_requests)
    await acc_mgr.load_all()
    logging.info("🚀 Bot started successfully...")
    await bot.run_until_disconnected()

if __name__ == '__main__':
    asyncio.run(main())