import re
import logging
from telethon import TelegramClient, events, Button, functions
from telethon.sessions import StringSession

logging.basicConfig(level=logging.INFO)

class AccountManager:
    def __init__(self, accounts_col, bot_client, api_id, api_hash, pending_requests, admin_ids=None, client_resolver=None):
        self.accounts_col = accounts_col
        self.bot = bot_client
        self.api_id = api_id
        self.api_hash = api_hash
        self.clients = {}
        self.pending_requests = pending_requests
        self.admin_ids = admin_ids or []
        # Optional callable: scope_id (str, "master" or a franchise_id) -> TelegramClient.
        # Lets OTPs be delivered via the SAME bot the customer actually bought
        # the account through, instead of always the master bot.
        self.client_resolver = client_resolver

    def _resolve_client(self, scope_id):
        if self.client_resolver:
            try:
                return self.client_resolver(scope_id)
            except Exception:
                pass
        return self.bot

    async def add_client(self, phone, session_str):
        if phone in self.clients:
            await self.remove_client(phone)
        client = TelegramClient(StringSession(session_str), self.api_id, self.api_hash)

        try:
            await client.connect()
        except Exception as e:
            logging.error(f"❌ Could not connect client for {phone}: {e}")
            return False

        try:
            authorized = await client.is_user_authorized()
        except Exception as e:
            logging.error(f"❌ Could not check authorization for {phone}: {e}")
            await client.disconnect()
            return False

        if not authorized:
            # Session is expired/invalid/logged-out. Never call client.start()
            # here without credentials — Telethon falls back to an interactive
            # input() prompt for phone/bot_token, which hangs a headless server.
            logging.error(f"❌ Session for {phone} is invalid/expired — skipping this account. "
                           f"Re-add it with a fresh session string.")
            await client.disconnect()
            try:
                await self.accounts_col.update_one(
                    {"phone": phone},
                    {"$set": {"status": "inactive"}}
                )
            except Exception as e:
                logging.error(f"Could not flag {phone} as inactive: {e}")

            for admin in self.admin_ids:
                try:
                    await self.bot.send_message(
                        admin,
                        f"⚠️ **Invalid Stock Detected on Startup!**\n"
                        f"📱 Phone: `{phone}`\n"
                        f"❌ Session is invalid/expired (logged out or revoked).\n"
                        f"🔄 Status: Marked as `inactive` in DB — replace this account's session."
                    )
                except Exception:
                    pass
            return False

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
                    key = (buyer_id, phone)
                    is_first_otp = not buyer_doc.get("first_otp_sent", False)

                    # First OTP after purchase delivers automatically. Every
                    # OTP after that only goes out if the buyer explicitly
                    # clicked "Request New OTP" — otherwise it's silently
                    # dropped (still consumed from Telegram, just not forwarded).
                    if not is_first_otp and key not in self.pending_requests:
                        logging.info(f"Suppressed unrequested OTP for {buyer_id} / {phone} "
                                     f"(no pending request on file).")
                        return

                    msg = f"📞 **Phone Number:** `{phone}`\n📩 **OTP:** `{otp}`"
                    twofa_password = buyer_doc.get("twofa_password")
                    if twofa_password:
                        msg += f"\n🔐 **Password:** `{twofa_password}`"
                    msg += "\n\n⚠️ Note: The Re‑Request button is active for 72 hours. After that, you'll need to request a new number."

                    # 🔥 Both buttons: Request New OTP & Logout from Bot
                    buttons = [[
                        Button.inline("🔄 Request New OTP", f"resend_{phone}"),
                        Button.inline("📱 Manage Sessions", f"open_sessions_{phone}")
                    ]]

                    sold_via = buyer_doc.get("sold_via_franchise_id", "master")
                    deliver_client = self._resolve_client(sold_via)
                    try:
                        await deliver_client.send_message(buyer_id, msg, buttons=buttons)
                    except Exception as e:
                        logging.error(f"Failed to send OTP to {buyer_id} via {sold_via}: {e}")

                    if is_first_otp:
                        await self.accounts_col.update_one(
                            {"_id": buyer_doc["_id"]},
                            {"$set": {"first_otp_sent": True}}
                        )

                    if key in self.pending_requests:
                        del self.pending_requests[key]
                        logging.info(f"Cleared pending OTP request for {buyer_id} / {phone}")

        logging.info(f"✅ Client started for {phone}")

    async def get_authorizations(self, phone):
        """Fetch the list of active device sessions (Authorization objects) for this account."""
        client = self.clients.get(phone)
        if not client:
            return None
        try:
            result = await client(functions.account.GetAuthorizationsRequest())
            return result.authorizations
        except Exception as e:
            logging.error(f"Failed to get authorizations for {phone}: {e}")
            return None

    async def terminate_session(self, phone, hash_id):
        """Terminate a single device session (by its hash) on the account, keeping the
        current session (the one this bot uses for OTP monitoring) intact."""
        client = self.clients.get(phone)
        if not client:
            return False, "No active connection for this number."
        try:
            await client(functions.account.ResetAuthorizationRequest(hash=hash_id))
            return True, "Session terminated."
        except Exception as e:
            return False, str(e)

    async def terminate_own_session(self, phone):
        """Log out the BOT'S OWN session for this account. Telegram does not allow
        terminating your own current session via ResetAuthorizationRequest (it only
        works on other devices' sessions) — the correct call is auth.LogOut, which
        Telethon exposes as client.log_out(). This revokes the session and
        disconnects, so OTP monitoring for this number stops permanently."""
        client = self.clients.get(phone)
        if not client:
            return False, "No active connection for this number."
        try:
            await client.log_out()
        except Exception as e:
            return False, str(e)
        finally:
            self.clients.pop(phone, None)
        try:
            await self.accounts_col.update_one(
                {"phone": phone},
                {"$set": {"status": "logged_out"}}
            )
        except Exception as e:
            logging.error(f"Could not flag {phone} as logged_out: {e}")
        return True, "Bot's session logged out."

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
            try:
                await self.add_client(acc["phone"], acc["session_string"])
            except Exception as e:
                logging.error(f"❌ Unexpected error loading account {acc.get('phone')}: {e}")
