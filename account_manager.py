import re
import logging
from telethon import TelegramClient, events, Button
from telethon.sessions import StringSession

logging.basicConfig(level=logging.INFO)

class AccountManager:
    def __init__(self, accounts_col, bot_client, api_id, api_hash, pending_requests):
        self.accounts_col = accounts_col
        self.bot = bot_client
        self.api_id = api_id
        self.api_hash = api_hash
        self.clients = {}
        self.pending_requests = pending_requests

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

                # 🔧 Always get the most recent buyer
                buyer_doc = await self.accounts_col.find_one(
                    {"phone": phone, "status": "sold"},
                    sort=[("sold_at", -1)]
                )
                buyer_id = buyer_doc["buyer_id"] if buyer_doc else None

                if buyer_id:
                    msg = f"📞 **Phone Number:** `{phone}`\n📩 **OTP:** `{otp}`"
                    twofa_password = buyer_doc.get("twofa_password")
                    if twofa_password:
                        msg += f"\n🔐 **Password:** `{twofa_password}`"
                    msg += "\n\n⚠️ Note: The Re‑Request button is active for 72 hours. After that, you'll need to request a new number."

                    # 🔥 Both buttons: Request New OTP & Logout from Bot
                    buttons = [[
                        Button.inline("🔄 Request New OTP", f"resend_{phone}"),
                        Button.inline("🔓 Logout from Bot", f"logout_{phone}")
                    ]]

                    try:
                        await self.bot.send_message(buyer_id, msg, buttons=buttons)
                    except Exception as e:
                        logging.error(f"Failed to send OTP to {buyer_id}: {e}")

                    key = (buyer_id, phone)
                    if key in self.pending_requests:
                        del self.pending_requests[key]
                        logging.info(f"Cleared pending OTP request for {buyer_id} / {phone}")

        logging.info(f"✅ Client started for {phone}")

    async def logout_client(self, phone):
        """Terminate the Telethon client for this phone number."""
        if phone in self.clients:
            await self.clients[phone].disconnect()
            del self.clients[phone]
            logging.info(f"Client for {phone} logged out by user request.")
        else:
            logging.warning(f"Attempt to logout non-existent client {phone}")

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
