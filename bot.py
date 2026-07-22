import os
import io
import asyncio
import logging
from datetime import datetime
from dotenv import load_dotenv
from telethon import TelegramClient, events, Button
from telethon.sessions import StringSession
from telethon.errors import (
    SessionPasswordNeededError,
    UserNotParticipantError,
    ChatAdminRequiredError,
    ChannelPrivateError
)
from motor.motor_asyncio import AsyncIOMotorClient
import qrcode
from bson import ObjectId
from account_manager import AccountManager

# ---------- .env LOAD ----------
load_dotenv()

API_ID = int(os.getenv("API_ID"))
API_HASH = os.getenv("API_HASH")
BOT_TOKEN = os.getenv("BOT_TOKEN")
MONGO_URL = os.getenv("MONGO_URL", "mongodb://localhost:27017")
ADMIN_IDS = [int(x.strip()) for x in os.getenv("ADMIN_IDS", "").split(",") if x.strip()]
UPI_ID = os.getenv("UPI_ID", "example@upi")
PAYEE_NAME = os.getenv("PAYEE_NAME", "OTPShop")
DEFAULT_PRICE = float(os.getenv("DEFAULT_PRICE", "50"))

# Force join (multiple channels, support for username & numeric ID)
FORCE_JOIN_SINGLE = os.getenv("FORCE_JOIN_CHAT_ID", "").strip()
FORCE_JOIN_LIST_RAW = os.getenv("FORCE_JOIN_CHAT_IDS", "").strip()
if FORCE_JOIN_LIST_RAW:
    RAW_CHAT_IDS = [x.strip() for x in FORCE_JOIN_LIST_RAW.split(",") if x.strip()]
elif FORCE_JOIN_SINGLE:
    RAW_CHAT_IDS = [FORCE_JOIN_SINGLE]
else:
    RAW_CHAT_IDS = []

if not all([API_ID, API_HASH, BOT_TOKEN, ADMIN_IDS]):
    raise ValueError("❌ .env file incomplete!")

logging.basicConfig(level=logging.INFO)

# ---------- MongoDB Setup ----------
mongo_client = AsyncIOMotorClient(MONGO_URL)
db = mongo_client['otp_bot']
accounts_col = db['accounts']
users_col = db['users']
orders_col = db['orders']
deposits_col = db['deposits']

# ---------- BOT INSTANCE ----------
bot = TelegramClient('bot_session', API_ID, API_HASH)

# ---------- STATE MACHINE ----------
user_states = {}
pending_otp_requests = {}

# ---------- HELPER: Get existing countries ----------
async def get_existing_countries():
    return await accounts_col.distinct("country", {})

# ---------- FORCE JOIN (IMPROVED) ----------
def parse_chat_id(raw_id: str):
    """Convert raw ID string to a format Telethon can use (str for username, int for numeric ID)."""
    raw = raw_id.strip()
    if raw.startswith('@'):
        return raw  # username, Telethon accepts as string
    try:
        chat_id_int = int(raw)
    except ValueError:
        logging.error(f"Invalid chat ID format: {raw}")
        return None
    # Telethon's get_entity can handle integer IDs (including -100 prefix)
    return chat_id_int

async def is_user_member(user_id: int) -> bool:
    """Check if user is member of all force-join chats. Returns True only if all passed."""
    if not RAW_CHAT_IDS:
        return True  # force join disabled
    for raw_id in RAW_CHAT_IDS:
        parsed = parse_chat_id(raw_id)
        if parsed is None:
            continue  # skip invalid IDs (already logged)
        try:
            entity = await bot.get_entity(parsed)
        except ValueError as e:
            logging.error(f"get_entity failed for '{raw_id}': {e}")
            return False
        except Exception as e:
            logging.error(f"Unexpected error resolving entity '{raw_id}': {type(e).__name__}: {e}")
            return False
        try:
            await bot.get_permissions(entity, user_id)
        except UserNotParticipantError:
            return False  # user not a member
        except ChatAdminRequiredError:
            logging.error(f"Bot is not admin in '{raw_id}'. Membership check impossible. Make the bot admin.")
            return False
        except ChannelPrivateError:
            logging.error(f"Bot cannot access private channel '{raw_id}'. Add bot as admin.")
            return False
        except Exception as e:
            logging.error(f"Error checking membership for '{raw_id}': {type(e).__name__}: {e}")
            return False
    return True

async def send_join_message(event):
    """Send a message listing required channels with Join buttons (public) and a Check Again button."""
    lines = []
    buttons = []
    for idx, raw_id in enumerate(RAW_CHAT_IDS, start=1):
        title = raw_id
        try:
            parsed = parse_chat_id(raw_id)
            entity = await bot.get_entity(parsed)
            title = getattr(entity, 'title', raw_id)
        except Exception as e:
            logging.warning(f"Could not get title for {raw_id}: {e}")
        if raw_id.startswith('@'):
            link = f"https://t.me/{raw_id[1:]}"
            lines.append(f"{idx}. [{title}]({link})")
            buttons.append([Button.url(f"📢 Join {title}", link)])
        else:
            lines.append(f"{idx}. Private: {title} (join manually)")
    join_text = (
        "🔒 **You must join these channels/groups to use the bot:**\n\n" +
        "\n".join(lines) +
        "\n\nAfter joining all, click the button below."
    )
    buttons.append([Button.inline("✅ Check Again", b"check_join")])
    await event.respond(join_text, buttons=buttons)

# ---------- MAIN MENU ----------
async def send_main_menu(event):
    user_id = event.sender_id
    if not await is_user_member(user_id):
        await send_join_message(event)
        return
    buttons = [
        [Button.inline("🛒 Buy Account", b"buy")],
        [Button.inline("💰 My Balance", b"balance")],
        [Button.inline("💳 Deposit", b"deposit")],
        [Button.inline("📜 Order History", b"orders")],
    ]
    if user_id in ADMIN_IDS:
        buttons.append([Button.inline("⚙️ Admin Panel", b"admin")])
    await event.respond("🌟 **OTP Bot Main Menu**", buttons=buttons)

# ---------- CALLBACK HANDLER ----------
@bot.on(events.CallbackQuery)
async def callback_handler(event):
    data = event.data.decode()
    user_id = event.sender_id

    # Always allow check_join
    if data == "check_join":
        if await is_user_member(user_id):
            await start_cmd(event)
        else:
            await event.answer("You haven't joined all channels yet!", alert=True)
        return

    # For all other callbacks, verify membership
    if not await is_user_member(user_id):
        await event.answer("You must join all channels first!", alert=True)
        await send_join_message(event)
        return

    # Top-level callbacks clear any existing state
    if data in ("main", "buy", "balance", "deposit", "orders", "admin",
                "admin_add_otp", "admin_add_sess", "admin_list", "admin_addbal",
                "admin_deposits", "admin_setprice"):
        user_states.pop(user_id, None)

    # --- User purchase flow ---
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
        user = await users_col.find_one({"user_id": user_id})
        balance = user["balance"] if user else 0
        if balance < price:
            await event.answer("❌ Insufficient balance!", alert=True)
            return

        acc = await accounts_col.find_one_and_update(
            {"country": country, "status": "available", "price": price},
            {"$set": {"status": "sold", "buyer_id": user_id, "sold_at": datetime.utcnow()}},
            sort=[("price", 1)]
        )
        if not acc:
            await event.answer("❌ Just sold out!", alert=True)
            return

        await users_col.update_one(
            {"user_id": user_id},
            {"$inc": {"balance": -price}},
            upsert=True
        )
        await orders_col.insert_one({
            "user_id": user_id,
            "account_id": str(acc["_id"]),
            "phone": acc["phone"],
            "country": country,
            "amount": price,
            "status": "completed",
            "created_at": datetime.utcnow()
        })

        phone = acc["phone"]
        twofa_password = acc.get("twofa_password")

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

        # Notify all admins
        for admin in ADMIN_IDS:
            try:
                await bot.send_message(admin,
                    f"🛒 **New Purchase**\n"
                    f"Buyer: `{user_id}`\n"
                    f"Phone: `{phone}`\n"
                    f"Country: {country}\n"
                    f"Price: ₹{price}\n"
                    f"Date: {datetime.utcnow().strftime('%d/%m/%Y %H:%M')}"
                )
            except:
                pass

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
        await event.edit("💵 Enter the amount you want to deposit (₹):",
                         buttons=[[Button.inline("🔙 Cancel", b"main")]])

    elif data == "orders":
        cursor = orders_col.find({"user_id": user_id}).sort("created_at", -1)
        orders = await cursor.to_list(length=10)
        if not orders:
            txt = "📜 No orders yet."
        else:
            txt = "📜 **Your Orders:**\n" + "\n".join(
                f"🔹 {o['phone']} ({o['country']}) - ₹{o['amount']} - {o['created_at'].strftime('%d/%m/%Y')}"
                for o in orders
            )
        await event.edit(txt, buttons=[[Button.inline("🔙 Back", b"main")]])

    # ---------- ADMIN CALLBACKS ----------
    elif data == "admin":
        if user_id not in ADMIN_IDS:
            await event.answer("❌ Unauthorized", alert=True)
            return
        btns = [
            [Button.inline("➕ Add Account (OTP)", b"admin_add_otp")],
            [Button.inline("📥 Add Account (Session)", b"admin_add_sess")],
            [Button.inline("📋 List Accounts", b"admin_list")],
            [Button.inline("💰 Add Balance", b"admin_addbal")],
            [Button.inline("💲 Set Price", b"admin_setprice")],
            [Button.inline("🕒 Pending Deposits", b"admin_deposits")],
            [Button.inline("🔙 Back", b"main")],
        ]
        await event.edit("⚙️ **Admin Panel**", buttons=btns)

    elif data == "admin_add_otp":
        await start_add_phone_flow(event)
    elif data == "admin_add_sess":
        await start_add_session_flow(event)

    elif data == "admin_list":
        cursor = accounts_col.find({})
        accounts = await cursor.to_list(length=100)
        if not accounts:
            txt = "No accounts."
        else:
            txt = "📋 **Accounts:**\n" + "\n".join(
                f"`{a['phone']}` | {a['country']} | {a['status']} | ₹{a.get('price', '?')}" +
                (f" (buyer:{a['buyer_id']})" if a.get('buyer_id') else "")
                for a in accounts
            )
        await event.edit(txt, buttons=[[Button.inline("🔙 Back", b"admin")]])

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
            btns.append([
                Button.inline(f"✅ Approve ₹{dep['amount']}", f"approve_{dep['_id']}"),
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
        await users_col.update_one(
            {"user_id": deposit["user_id"]},
            {"$inc": {"balance": deposit["amount"]}},
            upsert=True
        )
        await deposits_col.update_one({"_id": ObjectId(dep_id)}, {"$set": {"status": "approved"}})
        try:
            await bot.send_message(deposit["user_id"],
                                   f"✅ Deposit of ₹{deposit['amount']} approved! Balance updated.")
        except:
            pass
        await event.edit("✅ Deposit approved!", buttons=[[Button.inline("🔙 Admin Menu", b"admin")]])

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

    elif data == "main":
        await send_main_menu(event)

    # ---------- Country selection for admin add flows ----------
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

    else:
        await event.answer("Unknown action", alert=True)

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
            price = float(event.message.text)
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
            price = float(event.message.text)
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

# ---------- DEPOSIT FLOW (Screenshot) ----------
async def process_deposit_step(event):
    user_id = event.sender_id
    state = user_states.get(user_id)
    if not state or state["action"] != "deposit":
        return
    step = state["step"]
    if step == "amount":
        try:
            amount = float(event.message.text)
            if amount <= 0:
                raise ValueError
        except:
            await event.respond("❌ Invalid amount. Enter again:",
                                buttons=[[Button.inline("🔙 Cancel", b"main")]])
            return
        state["amount"] = amount
        upi_string = f"upi://pay?pa={UPI_ID}&pn={PAYEE_NAME}&am={amount}&tn=OTP_Deposit"
        img = qrcode.make(upi_string)
        buf = io.BytesIO()
        img.save(buf, format='PNG')
        buf.seek(0)
        buf.name = "qr_code.png"
        await bot.send_file(
            event.chat_id,
            buf,
            caption=f"💳 **Deposit ₹{amount}**\nScan QR or use UPI ID: `{UPI_ID}`\n\n"
                    "Payment karne ke baad uska **screenshot yahan bhejo**.",
            buttons=[[Button.inline("🔙 Cancel", b"main")]]
        )
        state["step"] = "screenshot"
    elif step == "screenshot":
        if not event.message.photo:
            await event.respond("❌ Kripya payment ka screenshot bhejein, text nahi.",
                                buttons=[[Button.inline("🔙 Cancel", b"main")]])
            return
        amount = state["amount"]
        result = await deposits_col.insert_one({
            "user_id": user_id,
            "amount": amount,
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
                    caption=f"🔔 **New Deposit Request**\nUser: `{user_id}`\nAmount: ₹{amount}\nProof: Screenshot",
                    buttons=[
                        [Button.inline("✅ Approve", f"approve_{dep_id}"),
                         Button.inline("❌ Reject", f"reject_{dep_id}")]
                    ])
                photo_io.seek(0)
            except:
                pass
        await event.respond(
            f"✅ Deposit request submitted!\nAmount: ₹{amount}\nAdmin will verify your screenshot and approve.",
            buttons=[[Button.inline("🔙 Main Menu", b"main")]]
        )
        user_states.pop(user_id, None)

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
                uid = int(event.message.text)
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
                amt = float(event.message.text)
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
    else:
        await send_main_menu(event)

# ---------- /start COMMAND ----------
@bot.on(events.NewMessage(pattern='/start'))
async def start_cmd(event):
    user_id = event.sender_id
    await users_col.update_one(
        {"user_id": user_id},
        {"$setOnInsert": {"balance": 0, "joined_at": datetime.utcnow()}},
        upsert=True
    )
    if not await is_user_member(user_id):
        await send_join_message(event)
        return
    welcome_msg = (
        "👋 **Welcome to the OTP Shop Bot!**\n\n"
        "🔐 **Buy Telegram Accounts** – Get login OTP & 2FA password instantly.\n"
        "💳 **Deposit via UPI/QR** – Send payment screenshot for approval.\n"
        "🌍 **Multiple Countries & Prices** – Choose country, see price‑wise stock.\n\n"
        "Use the buttons below to get started."
    )
    buttons = [
        [Button.inline("🛒 Buy Account", b"buy")],
        [Button.inline("💰 My Balance", b"balance")],
        [Button.inline("💳 Deposit", b"deposit")],
        [Button.inline("📜 Order History", b"orders")],
    ]
    if user_id in ADMIN_IDS:
        buttons.append([Button.inline("⚙️ Admin Panel", b"admin")])
    await event.respond(welcome_msg, buttons=buttons)

# ---------- MAIN FUNCTION ----------
async def main():
    await bot.start(bot_token=BOT_TOKEN)
    global acc_mgr
    acc_mgr = AccountManager(accounts_col, bot, API_ID, API_HASH, pending_otp_requests)
    await acc_mgr.load_all()
    logging.info("🚀 Bot started with bulletproof force join...")
    await bot.run_until_disconnected()

if __name__ == '__main__':
    asyncio.run(main())