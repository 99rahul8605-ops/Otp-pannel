import os
import io
import asyncio
import logging
from datetime import datetime
from dotenv import load_dotenv
from telethon import TelegramClient, events, Button
from telethon.sessions import StringSession
from telethon.errors import SessionPasswordNeededError
from motor.motor_asyncio import AsyncIOMotorClient
import qrcode
from bson import ObjectId
from account_manager import AccountManager   # <-- yahan import karo

# ---------- .env LOAD ----------
load_dotenv()

API_ID = int(os.getenv("API_ID"))
API_HASH = os.getenv("API_HASH")
BOT_TOKEN = os.getenv("BOT_TOKEN")
MONGO_URL = os.getenv("MONGO_URL", "mongodb://localhost:27017")
ADMIN_IDS = [int(x.strip()) for x in os.getenv("ADMIN_IDS", "").split(",") if x.strip()]
UPI_ID = os.getenv("UPI_ID", "example@upi")
PAYEE_NAME = os.getenv("PAYEE_NAME", "OTPShop")

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

# ---------- MAIN MENU ----------
async def send_main_menu(event):
    user_id = event.sender_id
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
            "phone": acc["phone"],
            "country": country,
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
                f"`{a['phone']}` | {a['country']} | {a['status']}" +
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
        # Add to account manager
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

# ---------- DEPOSIT FLOW (with QR) ----------
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
        await event.respond(
            file=buf,
            caption=f"💳 **Deposit ₹{amount}**\nScan QR or use UPI ID: `{UPI_ID}`\n\n"
                    "Payment karke Transaction ID yahan bhejo (ya 'done' type karo).",
            buttons=[[Button.inline("🔙 Cancel", b"main")]]
        )
        state["step"] = "txn_id"

    elif step == "txn_id":
        txn_id = event.message.text.strip()
        amount = state["amount"]
        await deposits_col.insert_one({
            "user_id": user_id,
            "amount": amount,
            "transaction_id": txn_id,
            "status": "pending",
            "created_at": datetime.utcnow()
        })
        await event.respond(
            f"✅ Deposit request sent!\nAmount: ₹{amount}\nTransaction ID: {txn_id}\n"
            "Admin will verify and approve shortly.",
            buttons=[[Button.inline("🔙 Main Menu", b"main")]]
        )
        user_states.pop(user_id, None)

# ---------- HANDLE ALL TEXT MESSAGES ----------
@bot.on(events.NewMessage(func=lambda e: e.is_private and not e.message.text.startswith('/')))
async def handle_message(event):
    user_id = event.sender_id
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
    await users_col.update_one(
        {"user_id": event.sender_id},
        {"$setOnInsert": {"balance": 0, "joined_at": datetime.utcnow()}},
        upsert=True
    )
    await send_main_menu(event)

# ---------- MAIN FUNCTION ----------
async def main():
    # Start bot client
    await bot.start(bot_token=BOT_TOKEN)

    # Initialize account manager with required references
    global acc_mgr
    acc_mgr = AccountManager(accounts_col, bot, API_ID, API_HASH)

    # Optional: create indexes
    # await accounts_col.create_index("phone", unique=True)
    # await users_col.create_index("user_id", unique=True)

    # Load all available account sessions
    await acc_mgr.load_all()

    logging.info("🚀 Bot started with separate OTP manager...")
    await bot.run_until_disconnected()

if __name__ == '__main__':
    asyncio.run(main())
