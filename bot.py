import os
import re
import asyncio
import logging
from datetime import datetime
from dotenv import load_dotenv
from telethon import TelegramClient, events, Button
from telethon.sessions import StringSession
from telethon.errors import SessionPasswordNeededError
from motor.motor_asyncio import AsyncIOMotorClient

# ---------- Load .env ----------
load_dotenv()

API_ID = int(os.getenv("API_ID"))
API_HASH = os.getenv("API_HASH")
BOT_TOKEN = os.getenv("BOT_TOKEN")
MONGO_URL = os.getenv("MONGO_URL", "mongodb://localhost:27017")
ADMIN_IDS = [int(x.strip()) for x in os.getenv("ADMIN_IDS", "").split(",") if x.strip()]

if not all([API_ID, API_HASH, BOT_TOKEN, ADMIN_IDS]):
    raise ValueError("Missing required environment variables. Check your .env file.")

logging.basicConfig(level=logging.INFO)

# ---------- MongoDB Setup ----------
mongo_client = AsyncIOMotorClient(MONGO_URL)
db = mongo_client['otp_bot']
accounts_col = db['accounts']
users_col = db['users']
orders_col = db['orders']

# ---------- ACCOUNT CLIENT MANAGER (OTP interceptor) ----------
class AccountManager:
    def __init__(self):
        self.clients = {}

    async def add_client(self, phone, session_str):
        if phone in self.clients:
            await self.remove_client(phone)
        client = TelegramClient(StringSession(session_str), API_ID, API_HASH)
        await client.start()
        self.clients[phone] = client

        @client.on(events.NewMessage(from_users=777000))
        async def otp_handler(event):
            text = event.message.message
            code_match = re.search(r'\b(\d{5,6})\b', text)
            if not code_match:
                code_match = re.search(r'Login code:\s*(\d+)', text, re.I)
            if code_match:
                otp = code_match.group(1)
                buyer = await accounts_col.find_one({"phone": phone, "status": "sold"})
                buyer_id = buyer["buyer_id"] if buyer else None
                if buyer_id:
                    try:
                        await bot.send_message(buyer_id, f"🔐 **Login OTP:** `{otp}`\n(Account: {phone})")
                    except:
                        pass
        logging.info(f"✅ Client started for {phone}")

    async def remove_client(self, phone):
        if phone in self.clients:
            await self.clients[phone].disconnect()
            del self.clients[phone]

    async def stop_all(self):
        for c in self.clients.values():
            await c.disconnect()
        self.clients.clear()

    async def load_all(self):
        async for acc in accounts_col.find({"status": "available"}):
            await self.add_client(acc["phone"], acc["session_string"])

acc_mgr = AccountManager()

# ---------- BOT ----------
bot = TelegramClient('bot_session', API_ID, API_HASH).start(bot_token=BOT_TOKEN)

# ---------- STATE MACHINE ----------
user_states = {}

# ---------- MAIN MENU ----------
async def send_main_menu(event):
    user_id = event.sender_id
    buttons = [
        [Button.inline("🛒 Buy Account", b"buy")],
        [Button.inline("💰 My Balance", b"balance")],
    ]
    if user_id in ADMIN_IDS:
        buttons.append([Button.inline("⚙️ Admin Panel", b"admin")])
    await event.respond("🌟 **OTP Bot Main Menu**", buttons=buttons)

# ---------- CALLBACK HANDLER ----------
@bot.on(events.CallbackQuery)
async def callback_handler(event):
    data = event.data.decode()
    user_id = event.sender_id
    user_states.pop(user_id, None)

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
        count = await accounts_col.count_documents({"country": country, "status": "available"})
        price = 50
        if count == 0:
            await event.answer("No accounts left.", alert=True)
            return
        btns = [
            [Button.inline(f"✅ Buy (₹{price})", f"confirm_{country}")],
            [Button.inline("🔙 Back", b"buy")],
        ]
        await event.edit(f"🌍 Country: {country}\n📦 Available: {count}\n💵 Price: {price}", buttons=btns)

    elif data.startswith("confirm_"):
        country = data.split("_", 1)[1]
        user = await users_col.find_one({"user_id": user_id})
        balance = user["balance"] if user else 0
        price = 50
        if balance < price:
            await event.answer("❌ Insufficient balance!", alert=True)
            return

        acc = await accounts_col.find_one_and_update(
            {"country": country, "status": "available"},
            {"$set": {"status": "sold", "buyer_id": user_id, "sold_at": datetime.utcnow()}}
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
            "amount": price,
            "status": "completed",
            "created_at": datetime.utcnow()
        })
        phone = acc["phone"]
        await event.edit(
            f"✅ **Purchase successful!**\n📱 Your number: `{phone}`\n\n"
            "Now login to Telegram with this number, OTP will appear here automatically.",
            buttons=[[Button.inline("🔙 Main Menu", b"main")]]
        )

    elif data == "balance":
        user = await users_col.find_one({"user_id": user_id})
        bal = user["balance"] if user else 0
        await event.edit(f"💰 Your balance: ₹{bal}", buttons=[[Button.inline("🔙 Back", b"main")]])

    elif data == "admin":
        if user_id not in ADMIN_IDS:
            await event.answer("❌ Unauthorized", alert=True)
            return
        btns = [
            [Button.inline("➕ Add Account (OTP)", b"admin_add_otp")],
            [Button.inline("📥 Add Account (Session)", b"admin_add_sess")],
            [Button.inline("📋 List Accounts", b"admin_list")],
            [Button.inline("💰 Add Balance", b"admin_addbal")],
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
                f"`{a['phone']}` | {a['country']} | {a['status']}" +
                (f" (buyer:{a['buyer_id']})" if a.get('buyer_id') else "")
                for a in accounts
            )
        await event.edit(txt, buttons=[[Button.inline("🔙 Back", b"admin")]])

    elif data == "admin_addbal":
        user_states[user_id] = {"action": "add_balance", "step": "await_user_id"}
        await event.edit("👤 Send the user ID:", buttons=[[Button.inline("🔙 Cancel", b"admin")]])

    elif data == "main":
        await send_main_menu(event)

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
        state["step"] = "country"
        await temp_client.disconnect()
        await event.respond("🌍 Send country code (e.g., IN, US):",
                            buttons=[[Button.inline("🔙 Cancel", b"admin")]])
    elif step == "2fa":
        password = event.message.text.strip()
        temp_client = state["temp_client"]
        try:
            await temp_client.sign_in(password=password)
            session_str = temp_client.session.save()
            state["session"] = session_str
            state["step"] = "country"
            await temp_client.disconnect()
            await event.respond("🌍 Send country code (e.g., IN, US):",
                                buttons=[[Button.inline("🔙 Cancel", b"admin")]])
        except Exception as e:
            await temp_client.disconnect()
            await event.respond(f"❌ 2FA failed: {str(e)}", buttons=[[Button.inline("🔙 Cancel", b"admin")]])
            user_states.pop(user_id, None)
    elif step == "country":
        country = event.message.text.strip().upper()
        session_str = state["session"]
        phone = state["phone"]
        await accounts_col.insert_one({
            "phone": phone,
            "country": country,
            "session_string": session_str,
            "status": "available"
        })
        await acc_mgr.add_client(phone, session_str)
        await event.respond(f"✅ Account `{phone}` ({country}) added successfully!",
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
    if state["step"] == "session":
        session_str = event.message.text.strip()
        temp_client = TelegramClient(StringSession(session_str), API_ID, API_HASH)
        try:
            await temp_client.connect()
            if not await temp_client.is_user_authorized():
                await event.respond("❌ Invalid session!", buttons=[[Button.inline("🔙 Cancel", b"admin")]])
                await temp_client.disconnect()
                user_states.pop(user_id, None)
                return
            me = await temp_client.get_me()
            phone = me.phone
            state["phone"] = phone
            state["session"] = session_str
            state["step"] = "country"
            await temp_client.disconnect()
            await event.respond(f"📱 Number: {phone}\n🌍 Send country code (e.g., IN):",
                                buttons=[[Button.inline("🔙 Cancel", b"admin")]])
        except Exception as e:
            await event.respond(f"❌ Error: {str(e)}", buttons=[[Button.inline("🔙 Cancel", b"admin")]])
            user_states.pop(user_id, None)
    elif state["step"] == "country":
        country = event.message.text.strip().upper()
        phone = state["phone"]
        session_str = state["session"]
        await accounts_col.insert_one({
            "phone": phone,
            "country": country,
            "session_string": session_str,
            "status": "available"
        })
        await acc_mgr.add_client(phone, session_str)
        await event.respond(f"✅ Account `{phone}` ({country}) added!",
                            buttons=[[Button.inline("🔙 Admin Menu", b"admin")]])
        user_states.pop(user_id, None)

# ---------- HANDLE ALL MESSAGES ----------
@bot.on(events.NewMessage(func=lambda e: e.is_private and not e.message.text.startswith('/')))
async def handle_message(event):
    user_id = event.sender_id
    text = event.message.text
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
                uid = int(text)
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
                amt = float(text)
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

# ---------- /start command ----------
@bot.on(events.NewMessage(pattern='/start'))
async def start_cmd(event):
    await users_col.update_one(
        {"user_id": event.sender_id},
        {"$setOnInsert": {"balance": 0, "joined_at": datetime.utcnow()}},
        upsert=True
    )
    await send_main_menu(event)

# ---------- MAIN ----------
async def main():
    # Optional: unique index creation
    # await accounts_col.create_index("phone", unique=True)
    # await users_col.create_index("user_id", unique=True)
    await acc_mgr.load_all()
    logging.info("Bot started with .env, MongoDB and inline buttons...")
    await bot.run_until_disconnected()

if __name__ == '__main__':
    asyncio.run(main())
