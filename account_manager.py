import re
import logging
from telethon import TelegramClient, events
from telethon.sessions import StringSession

logging.basicConfig(level=logging.INFO)

class AccountManager:
    def __init__(self, accounts_col, bot_client, api_id, api_hash, pending_requests):
        self.accounts_col = accounts_col
        self.bot = bot_client
        self.api_id = api_id
        self.api_hash = api_hash
        self.clients = {}
        self.pending_requests = pending_requests  # dict (user_id, phone) -> bool

    async def add_client(self, phone, session_str):
        if phone in self.clients:
            await self.remove_client(phone)
        client = TelegramClient(StringSession(session_str), self.api_id, self.api_hash)
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

                # 🔧 FIX: find the MOST RECENT buyer for this phone (sort by sold_at descending)
                buyer_doc = await self.accounts_col.find_one(
                    {"phone": phone, "status": "sold"},
                    sort=[("sold_at", -1)]
                )
                buyer_id = buyer_doc["buyer_id"] if buyer_doc else None

                if buyer_id:
                    try:
                        await self.bot.send_message(buyer_id, f"🔐 **Login OTP:** `{otp}`\n(Account: {phone})")
                    except Exception as e:
                        logging.error(f"Failed to send OTP to {buyer_id}: {e}")

                    # If there's a pending resend request for this user+phone, clear it
                    key = (buyer_id, phone)
                    if key in self.pending_requests:
                        del self.pending_requests[key]
                        logging.info(f"Cleared pending OTP request for {buyer_id} / {phone}")

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
        async for acc in self.accounts_col.find({"status": "available"}):
            await self.add_client(acc["phone"], acc["session_string"])
