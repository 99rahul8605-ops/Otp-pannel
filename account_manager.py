import asyncio
import logging
import os
import random
import re
import socket
import uuid
from datetime import datetime, timedelta, timezone

from telethon import TelegramClient, events, Button, functions, errors
from telethon.sessions import StringSession
from pymongo.errors import DuplicateKeyError

logging.basicConfig(level=logging.INFO)

# ---------------------------------------------------------------------------
# Purchased-account validity
# ---------------------------------------------------------------------------
# The window starts at the purchase timestamp (``sold_at``) and the resulting
# expiry is persisted on the account document as ``access_expires_at``. It is
# only ever written once per sale (sale code sets it, the startup back-fill only
# fills *missing* values), so restarts can neither reset nor extend it.
ACCESS_VALIDITY = timedelta(hours=72)

_IST = timezone(timedelta(hours=5, minutes=30))


def _default_now():
    """Naive IST datetime, same convention as bot.now_ist() / existing fields."""
    return datetime.now(_IST).replace(tzinfo=None)


def compute_access_expiry(sold_at, validity=ACCESS_VALIDITY):
    return sold_at + validity


# ---------------------------------------------------------------------------
# Connection states (persisted in MongoDB as ``conn_status``)
# ---------------------------------------------------------------------------
CONNECTED = "connected"
RECONNECTING = "reconnecting"
DISCONNECTED = "disconnected"
RATE_LIMITED = "rate_limited"
# Another deployment/process currently owns this phone's Telegram session. This
# is a transient, in-memory-only state: it is never written to MongoDB, because
# the owner's persisted status is the truth.
STANDBY = "standby"
INVALID_SESSION = "invalid_session"
EXPIRED = "expired"

# Telegram says this session can never work again.
_INVALID_SESSION_ERRORS = tuple(
    getattr(errors, n)
    for n in (
        "AuthKeyUnregisteredError",
        "SessionRevokedError",
        "SessionExpiredError",
        "UserDeactivatedError",
        "UserDeactivatedBanError",
        "AuthKeyDuplicatedError",
    )
    if hasattr(errors, n)
)


class _InvalidSession(Exception):
    """Raised internally when Telegram reports the session as not authorized."""


def classify_error(exc):
    """Return one of 'invalid', 'flood', 'network', 'other'."""
    if isinstance(exc, (_InvalidSession,) + _INVALID_SESSION_ERRORS):
        return "invalid"
    if isinstance(exc, errors.FloodWaitError):
        return "flood"
    if isinstance(exc, (asyncio.TimeoutError, TimeoutError, ConnectionError, OSError)):
        return "network"
    server_err = getattr(errors, "ServerError", None)
    timed_out = getattr(errors, "TimedOutError", None)
    if server_err and isinstance(exc, server_err):
        return "network"
    if timed_out and isinstance(exc, timed_out):
        return "network"
    return "other"


def _result(ok, state, message, **extra):
    out = {"ok": ok, "state": state, "message": message,
           "expires_at": None, "already_waiting": False}
    out.update(extra)
    return out


class AccountManager:
    def __init__(
        self,
        accounts_col,
        bot_client,
        api_id,
        api_hash,
        pending_requests,
        admin_ids=None,
        client_resolver=None,
        otp_sent_notifier=None,
        *,
        validity=ACCESS_VALIDITY,
        clock=None,
        sleep=None,
        client_factory=None,
        max_attempts=4,
        interactive_attempts=2,
        backoff_base=2.0,
        backoff_cap=60.0,
        backoff_jitter=0.2,
        connect_timeout=20.0,
        max_inline_flood_wait=30,
        supervisor_interval=60.0,
        supervisor_cooldown_cap=900.0,
        restore_concurrency=5,
        rng=None,
        leases_col=None,
        instance_id=None,
        lease_ttl=120.0,
        lease_heartbeat=30.0,
        health_check_interval=600.0,
    ):
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
        # Optional async callback invoked only after an OTP was successfully
        # delivered to the buyer. Kept optional for backwards compatibility.
        self.otp_sent_notifier = otp_sent_notifier

        self.validity = validity
        self._now = clock or _default_now
        self._sleep = sleep or asyncio.sleep
        self._client_factory = client_factory or self._default_client_factory
        self.max_attempts = max(1, int(max_attempts))
        self.interactive_attempts = max(1, int(interactive_attempts))
        self.backoff_base = backoff_base
        self.backoff_cap = backoff_cap
        self.backoff_jitter = backoff_jitter
        self.connect_timeout = connect_timeout
        self.max_inline_flood_wait = max_inline_flood_wait
        self.supervisor_interval = supervisor_interval
        self.supervisor_cooldown_cap = supervisor_cooldown_cap
        self.restore_concurrency = max(1, int(restore_concurrency))
        self._rng = rng or random.random

        # One lock per phone: serialises connect / reconnect / remove / expire so
        # two tasks can never build two clients (or two handlers) for one number.
        self._locks = {}
        self._conn_state = {}          # phone -> last persisted (status, error, retry_after)
        self._failed_cycles = {}       # phone -> consecutive failed reconnect cycles
        self._retry_not_before = {}    # phone -> datetime (supervisor cool-down)
        self._supervisor = None
        self._lease_task = None

        # Cross-process ownership. asyncio.Lock only protects ONE process; during
        # a rolling redeploy the old and new process overlap, so each phone's
        # Telegram session is guarded by a MongoDB lease (collection
        # ``account_leases``, _id = phone). Only the lease holder may keep a
        # client connected / forward OTPs.
        if leases_col is None:
            try:
                leases_col = accounts_col.database["account_leases"]
            except Exception:
                leases_col = None
                logging.warning("No MongoDB database handle: distributed session leases DISABLED "
                                "(safe only with a single running instance).")
        self.leases = leases_col
        self.instance_id = instance_id or f"{socket.gethostname()}:{os.getpid()}:{uuid.uuid4().hex[:8]}"
        self.lease_ttl = float(lease_ttl)
        self.lease_heartbeat = float(lease_heartbeat)
        self.health_check_interval = float(health_check_interval)
        self._lease_until = {}     # phone -> local deadline of the lease we hold
        self._standby = set()      # phones another instance owned when we tried
        self._verified_at = {}     # phone -> last successful live authorization check

    # ------------------------------------------------------------------ utils
    def _default_client_factory(self, session_str):
        # connection_retries is deliberately small: the outer loop owns the
        # bounded retry / exponential backoff policy.
        return TelegramClient(
            StringSession(session_str), self.api_id, self.api_hash,
            connection_retries=2, retry_delay=1, auto_reconnect=True,
        )

    def _resolve_client(self, scope_id):
        if self.client_resolver:
            try:
                return self.client_resolver(scope_id)
            except Exception:
                pass
        return self.bot

    def _lock(self, phone):
        lock = self._locks.get(phone)
        if lock is None:
            lock = self._locks[phone] = asyncio.Lock()
        return lock

    def _backoff(self, attempt):
        delay = min(self.backoff_cap, self.backoff_base * (2 ** (attempt - 1)))
        jitter = self.backoff_jitter * (self._rng() * 2 - 1)
        return max(0.0, delay * (1 + jitter))

    async def _safe_disconnect(self, client):
        try:
            await asyncio.wait_for(client.disconnect(), 10)
        except Exception as e:
            logging.warning(f"Client disconnect problem (ignored): {e}")

    # ------------------------------------------------------ distributed lease
    def _owns(self, phone):
        """True while we provably hold the lease (local deadline not passed)."""
        if self.leases is None:
            return True
        until = self._lease_until.get(phone)
        return until is not None and until > self._now()

    async def _acquire_lease(self, phone):
        if self.leases is None:
            return True
        now = self._now()
        expires = now + timedelta(seconds=self.lease_ttl)
        try:
            # Take it if we already own it or the previous owner's lease lapsed.
            res = await self.leases.update_one(
                {"_id": phone, "$or": [{"owner": self.instance_id}, {"expires_at": {"$lte": now}}]},
                {"$set": {"owner": self.instance_id, "expires_at": expires, "renewed_at": now}},
            )
            ok = res.matched_count > 0
            if not ok:
                try:
                    await self.leases.insert_one(
                        {"_id": phone, "owner": self.instance_id, "expires_at": expires,
                         "acquired_at": now, "renewed_at": now})
                    ok = True
                except DuplicateKeyError:
                    ok = False      # a live lease held by someone else
        except Exception as e:
            logging.error(f"Lease acquire failed for {phone}: {e}")
            return False            # fail closed: never connect without ownership
        if ok:
            self._lease_until[phone] = expires
            self._standby.discard(phone)
        return ok

    async def _release_lease(self, phone):
        self._lease_until.pop(phone, None)
        if self.leases is None:
            return
        try:
            await self.leases.delete_one({"_id": phone, "owner": self.instance_id})
        except Exception as e:
            logging.warning(f"Lease release failed for {phone} (it will lapse by TTL): {e}")

    async def renew_leases(self):
        """Heartbeat. A lease we can no longer prove we hold means someone else may
        connect this session, so the local client is dropped immediately."""
        if self.leases is None:
            return 0
        lost = 0
        for phone in list(self._lease_until):
            now = self._now()
            holds = None
            try:
                res = await self.leases.update_one(
                    {"_id": phone, "owner": self.instance_id},
                    {"$set": {"expires_at": now + timedelta(seconds=self.lease_ttl), "renewed_at": now}},
                )
                holds = res.matched_count > 0
            except Exception as e:
                logging.warning(f"Lease renew error for {phone}: {e}")
            if holds:
                self._lease_until[phone] = now + timedelta(seconds=self.lease_ttl)
                continue
            if holds is None and self._lease_until.get(phone, now) > now:
                continue            # transient DB error, local deadline still valid
            logging.error(f"Lost ownership lease for {phone}; dropping local client.")
            self._lease_until.pop(phone, None)
            client = self.clients.pop(phone, None)
            if client is not None:
                await self._safe_disconnect(client)
            self._standby.add(phone)
            lost += 1
        return lost

    async def retry_standby(self):
        """Retry phones another instance owned (e.g. the old deployment shutting down)."""
        n = 0
        for phone in list(self._standby):
            state, _ = await self.ensure_connected(phone, blocking=False)
            if state == CONNECTED:
                n += 1
            if state != STANDBY:
                self._standby.discard(phone)
        return n

    # --------------------------------------------------------------- validity
    def expiry_of(self, doc):
        """Persisted expiry, falling back to sold_at + validity for legacy docs."""
        exp = doc.get("access_expires_at")
        if isinstance(exp, datetime):
            return exp
        sold_at = doc.get("sold_at")
        if isinstance(sold_at, datetime):
            return compute_access_expiry(sold_at, self.validity)
        return None

    def _is_expired(self, doc):
        if doc.get("access_expired") is True:
            return True
        exp = self.expiry_of(doc)
        if exp is None:
            return True  # no purchase timestamp -> cannot prove validity -> fail closed
        return exp <= self._now()

    @staticmethod
    def _should_monitor_sold(doc):
        """Sold accounts that need a live OTP listener."""
        if not doc.get("session_string"):
            return False  # e.g. session_zip bulk orders: session string was removed
        if doc.get("bulk_mode") == "direct_otp" and not doc.get("bulk_active", False):
            return False  # queued bulk number stays silent until activated
        return True

    async def backfill_expiry(self):
        """Give legacy sold accounts a persistent expiry derived from sold_at.

        Only touches documents with *no* ``access_expires_at`` and uses the same
        filter inside the update, so running it on every boot is idempotent and
        can never move an existing expiry.
        """
        n = 0
        async for d in self.accounts_col.find(
            {"status": "sold", "access_expires_at": {"$exists": False}, "sold_at": {"$ne": None}}
        ):
            sold_at = d.get("sold_at")
            if not isinstance(sold_at, datetime):
                continue
            res = await self.accounts_col.update_one(
                {"_id": d["_id"], "access_expires_at": {"$exists": False}},
                {"$set": {"access_expires_at": compute_access_expiry(sold_at, self.validity)}},
            )
            n += res.modified_count
        if n:
            logging.info(f"Back-filled access_expires_at for {n} sold account(s).")
        return n

    async def expire_account(self, doc):
        """Disable a purchased account whose window ended and free its resources.

        The DB flip is an atomic claim, so concurrent callers (supervisor, OTP
        handler, Request Again) cleanup exactly once. The sale record itself
        (status="sold", buyer, price...) is untouched; the Telegram session is
        NOT logged out - we only stop using it.
        """
        now = self._now()
        res = await self.accounts_col.update_one(
            {"_id": doc["_id"], "status": "sold", "access_expired": {"$ne": True}},
            {"$set": {"access_expired": True, "access_expired_at": now,
                      "conn_status": EXPIRED, "conn_updated_at": now,
                      "conn_retry_after": None}},
        )
        if res.modified_count == 0:
            return False
        phone = doc.get("phone")
        buyer_id = doc.get("buyer_id")
        self.pending_requests.pop((buyer_id, phone), None)
        self._conn_state[phone] = (EXPIRED, None, None)
        self._failed_cycles.pop(phone, None)
        self._retry_not_before.pop(phone, None)
        try:
            async with self._lock(phone):
                other = await self.accounts_col.find_one({
                    "phone": phone, "_id": {"$ne": doc["_id"]},
                    "$or": [{"status": "available"},
                            {"status": "sold", "access_expired": {"$ne": True}}],
                })
                if other is None:
                    await self._remove_locked(phone)
        except Exception as e:
            logging.error(f"Cleanup after expiry failed for {phone}: {e}")
        logging.info(f"⏰ Purchased account {phone} expired; client released.")
        return True

    async def expire_due(self):
        now = self._now()
        docs = await self.accounts_col.find({
            "status": "sold", "access_expired": {"$ne": True},
            "$or": [
                {"access_expires_at": {"$lte": now}},
                {"access_expires_at": {"$exists": False}, "sold_at": {"$lte": now - self.validity}},
            ],
        }).to_list(length=None)
        count = 0
        for d in docs:
            try:
                if await self.expire_account(d):
                    count += 1
            except Exception as e:
                logging.error(f"Expiry failed for {d.get('phone')}: {e}")
        return count

    # ------------------------------------------------------- persisted status
    async def _set_conn_state(self, phone, status, error=None, retry_after=None):
        key = (status, error, retry_after)
        if self._conn_state.get(phone) == key:
            return
        self._conn_state[phone] = key
        now = self._now()
        fields = {"conn_status": status, "conn_updated_at": now,
                  "conn_last_error": error, "conn_retry_after": retry_after}
        if status == CONNECTED:
            fields["conn_last_ok_at"] = now
        try:
            await self.accounts_col.update_many(
                {"phone": phone, "status": {"$in": ["available", "sold"]},
                 "access_expired": {"$ne": True}},
                {"$set": fields},
            )
        except Exception as e:
            logging.error(f"Could not persist connection state for {phone}: {e}")

    def _cached_status(self, phone):
        v = self._conn_state.get(phone)
        return v[0] if v else None

    async def _find_doc(self, phone, sold_only=False):
        q = {"phone": phone}
        if sold_only:
            q["status"] = "sold"
        return await self.accounts_col.find_one(q, sort=[("sold_at", -1)])

    # ------------------------------------------------------ connection core
    def _make_otp_handler(self, phone):
        async def otp_handler(event):
            await self._on_otp(phone, event)
        return otp_handler

    async def _remove_locked(self, phone, release=True):
        client = self.clients.pop(phone, None)
        if client is not None:
            await self._safe_disconnect(client)
        self._verified_at.pop(phone, None)
        if release:
            await self._release_lease(phone)

    async def _attempt_locked(self, phone, session_str, force_new=False):
        """One connection attempt. Caller holds the phone lock.

        Raises on failure; on any failure/cancellation the half-built client is
        disconnected and never stored, so no stale or duplicate client survives.
        """
        client = None if force_new else self.clients.get(phone)
        created = False
        if client is None:
            # Always tear down a previous client BEFORE creating a new one: two
            # live clients on one auth key makes Telegram revoke the session.
            await self._remove_locked(phone, release=False)
            client = self._client_factory(session_str)
            client.add_event_handler(
                self._make_otp_handler(phone), events.NewMessage(from_users=777000)
            )
            created = True
        try:
            if not client.is_connected():
                await asyncio.wait_for(client.connect(), self.connect_timeout)
            authorized = await asyncio.wait_for(client.is_user_authorized(), self.connect_timeout)
            if not authorized:
                raise _InvalidSession("session is not authorized")
        except BaseException:
            await self._safe_disconnect(client)
            if self.clients.get(phone) is client:
                self.clients.pop(phone, None)
            raise
        self.clients[phone] = client
        return created

    async def _mark_invalid_locked(self, phone, reason):
        await self._remove_locked(phone)
        previous = self._cached_status(phone)
        doc = await self._find_doc(phone)
        was_sold = bool(doc and doc.get("status") == "sold")
        try:
            if doc and doc.get("status") == "available":
                # Unsold stock: keep the original behaviour (pull it from sale).
                await self.accounts_col.update_one(
                    {"_id": doc["_id"], "status": "available"},
                    {"$set": {"status": "inactive"}},
                )
        except Exception as e:
            logging.error(f"Could not flag {phone} as inactive: {e}")
        await self._set_conn_state(phone, INVALID_SESSION, reason)
        if previous == INVALID_SESSION:
            return
        label = "Purchased account session invalid" if was_sold else "Invalid Stock Detected"
        action = ("Buyer's Request Again will report an invalid session; replace/refund as needed."
                  if was_sold else
                  "Status: Marked as `inactive` in DB — replace this account's session.")
        for admin in self.admin_ids:
            try:
                await self.bot.send_message(
                    admin,
                    f"⚠️ **{label}!**\n"
                    f"📱 Phone: `{phone}`\n"
                    f"❌ Session is invalid/expired (logged out or revoked).\n"
                    f"🔄 {action}"
                )
            except Exception:
                pass

    async def _verify_locked(self, phone, client):
        """Live Telegram authorization check (a real request, not just socket state).

        Returns ("ok"|"invalid"|"stale", detail). "stale" = could not verify
        (network/flood/other): the client must be rebuilt, not trusted.
        """
        try:
            authorized = await asyncio.wait_for(client.is_user_authorized(), self.connect_timeout)
        except asyncio.CancelledError:
            raise
        except Exception as e:
            detail = f"{type(e).__name__}: {e}"[:200]
            if classify_error(e) == "invalid":
                await self._mark_invalid_locked(phone, detail)
                return "invalid", detail
            return "stale", detail
        if not authorized:
            await self._mark_invalid_locked(phone, "session is not authorized")
            return "invalid", "session is not authorized"
        self._verified_at[phone] = self._now()
        return "ok", None

    async def _ensure_locked(self, phone, session_str, max_attempts):
        client = self.clients.get(phone)
        if client is not None:
            if client.is_connected() and self._owns(phone):
                # A connected socket is NOT proof of a healthy session: verify
                # authorization with Telegram before reporting CONNECTED.
                verdict, detail = await self._verify_locked(phone, client)
                if verdict == "ok":
                    if self._cached_status(phone) != CONNECTED:
                        await self._set_conn_state(phone, CONNECTED)
                    return CONNECTED, None
                if verdict == "invalid":
                    self._standby.discard(phone)
                    return INVALID_SESSION, detail
                logging.warning(f"Client for {phone} could not be verified ({detail}); rebuilding.")
            # stale, unverifiable or no-longer-owned client: drop it before anything else
            await self._remove_locked(phone, release=False)

        if session_str is None:
            doc = await self._find_doc(phone)
            if doc is None or doc.get("status") not in ("available", "sold"):
                self._standby.discard(phone)
                await self._release_lease(phone)
                return DISCONNECTED, "account not eligible for monitoring"
            if doc.get("status") == "sold" and self._is_expired(doc):
                self._standby.discard(phone)
                await self._release_lease(phone)
                return EXPIRED, "validity ended"
            session_str = doc.get("session_string")
            if not session_str:
                self._standby.discard(phone)
                return INVALID_SESSION, "session missing"

        if not await self._acquire_lease(phone):
            self._standby.add(phone)
            logging.info(f"{phone} is owned by another instance; standing by.")
            return STANDBY, "session is owned by another instance"

        await self._set_conn_state(phone, RECONNECTING)
        last_err = None
        for attempt in range(1, max_attempts + 1):
            try:
                # A client that is not connected is stale: rebuild it (the old one is
                # disconnected first) instead of reviving a possibly broken object.
                await self._attempt_locked(phone, session_str, force_new=True)
            except asyncio.CancelledError:
                raise
            except Exception as e:
                kind = classify_error(e)
                last_err = f"{type(e).__name__}: {e}"[:200]
                logging.warning(f"Connect attempt {attempt}/{max_attempts} for {phone} failed ({kind}): {last_err}")
                if kind == "invalid":
                    await self._mark_invalid_locked(phone, last_err)
                    return INVALID_SESSION, last_err
                if kind == "flood":
                    wait = int(getattr(e, "seconds", 0) or 0)
                    if wait <= self.max_inline_flood_wait and attempt < max_attempts:
                        await self._sleep(wait + 1)
                        continue
                    until = self._now() + timedelta(seconds=wait)
                    await self._set_conn_state(phone, RATE_LIMITED, last_err, until)
                    await self._release_lease(phone)
                    return RATE_LIMITED, last_err
                if attempt < max_attempts:
                    await self._sleep(self._backoff(attempt))
                continue
            self._failed_cycles.pop(phone, None)
            self._retry_not_before.pop(phone, None)
            self._verified_at[phone] = self._now()
            await self._set_conn_state(phone, CONNECTED)
            return CONNECTED, None

        n = self._failed_cycles[phone] = self._failed_cycles.get(phone, 0) + 1
        cooldown = min(self.supervisor_cooldown_cap, self.supervisor_interval * (2 ** (n - 1)))
        self._retry_not_before[phone] = self._now() + timedelta(seconds=cooldown)
        await self._set_conn_state(phone, DISCONNECTED, last_err)
        await self._release_lease(phone)
        return DISCONNECTED, last_err

    async def ensure_connected(self, phone, *, session_str=None, max_attempts=None, blocking=True):
        """Make sure exactly one healthy client exists for ``phone``.

        Returns ``(state, detail)``. With ``blocking=False`` a call that finds a
        reconnect already running returns RECONNECTING immediately instead of
        queueing behind it.
        """
        lock = self._lock(phone)
        if not blocking and lock.locked():
            return RECONNECTING, "reconnect already in progress"
        async with lock:
            return await self._ensure_locked(phone, session_str, max_attempts or self.max_attempts)

    async def add_client(self, phone, session_str):
        """Open a client from an explicit session string (single attempt).

        Replaces any existing client for the phone. Returns True/False like
        before. Retry/backoff policy lives in ensure_connected().
        """
        async with self._lock(phone):
            if not await self._acquire_lease(phone):
                logging.error(f"❌ {phone} is owned by another running instance; not connecting here.")
                return False
            try:
                await self._attempt_locked(phone, session_str, force_new=True)
            except asyncio.CancelledError:
                raise
            except Exception as e:
                kind = classify_error(e)
                detail = f"{type(e).__name__}: {e}"[:200]
                if kind == "invalid":
                    logging.error(f"❌ Session for {phone} is invalid/expired — skipping this account. "
                                  f"Re-add it with a fresh session string.")
                    await self._mark_invalid_locked(phone, detail)
                else:
                    logging.error(f"❌ Could not connect client for {phone}: {detail}")
                    state = RATE_LIMITED if kind == "flood" else DISCONNECTED
                    await self._set_conn_state(phone, state, detail)
                    await self._release_lease(phone)
                return False
            self._verified_at[phone] = self._now()
            await self._set_conn_state(phone, CONNECTED)
            logging.info(f"✅ Client started for {phone}")
            return True

    async def _on_otp(self, phone, event):
        if not self._owns(phone):
            # Fencing: if we cannot prove we own this session (lease lapsed, e.g.
            # after a long pause), another instance may be serving it. Stay silent.
            logging.warning(f"Ignoring OTP event for {phone}: lease not held by this instance.")
            return
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

            # Validity gate: after the 72h window no OTP is forwarded and the
            # resources are released (idempotent, safe under races).
            if buyer_doc and self._is_expired(buyer_doc):
                await self.expire_account(buyer_doc)
                logging.info(f"Dropped OTP for expired account {phone}.")
                return

            if buyer_id:
                key = (buyer_id, phone)

                # In Direct-OTP bulk mode only the currently issued number
                # is allowed to forward its first OTP. Queued numbers remain
                # silent until the previous number completes.
                if buyer_doc.get("bulk_order_id") and buyer_doc.get("bulk_mode") == "direct_otp" and not buyer_doc.get("bulk_active", False):
                    logging.info(
                        f"Suppressed OTP for queued bulk account {buyer_id} / {phone}."
                    )
                    return

                is_first_otp = not buyer_doc.get("first_otp_sent", False)

                # First OTP after purchase delivers automatically. Every
                # OTP after that only goes out if the buyer explicitly
                # clicked "Request New OTP" — otherwise it's silently
                # dropped (still consumed from Telegram, just not forwarded).
                if not is_first_otp and key not in self.pending_requests:
                    logging.info(f"Suppressed unrequested OTP for {buyer_id} / {phone} "
                                 f"(no pending request on file).")
                    return

                if buyer_doc.get("bulk_order_id") and buyer_doc.get("bulk_mode") == "direct_otp":
                    bulk_index = int(buyer_doc.get("bulk_index", 0) or 0)
                    bulk_total = int(buyer_doc.get("bulk_total", 0) or 0)
                    msg = f"📦 **Bulk OTP {bulk_index}/{bulk_total}**\n\n📞 **Phone Number:** `{phone}`\n📩 **OTP:** `{otp}`"
                else:
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
                delivered = False
                try:
                    await deliver_client.send_message(buyer_id, msg, buttons=buttons)
                    delivered = True
                except Exception as e:
                    logging.error(f"Failed to send OTP to {buyer_id} via {sold_via}: {e}")

                # Notify the correct bot owner/admin only after the buyer
                # actually received the OTP message.
                if delivered and self.otp_sent_notifier:
                    try:
                        await self.otp_sent_notifier(
                            scope_id=sold_via,
                            buyer_id=buyer_id,
                            phone=phone,
                            otp=otp,
                            server_name="Server 1",
                            country=buyer_doc.get("country", "N/A"),
                            is_refresh=not is_first_otp,
                        )
                    except Exception as e:
                        logging.error(
                            f"OTP owner notification failed for {buyer_id} / {phone}: {e}"
                        )

                if is_first_otp and delivered:
                    await self.accounts_col.update_one(
                        {"_id": buyer_doc["_id"]},
                        {"$set": {"first_otp_sent": True}}
                    )

                if delivered and key in self.pending_requests:
                    del self.pending_requests[key]
                    logging.info(f"Cleared pending OTP request for {buyer_id} / {phone}")


    # ------------------------------------------------------------ validation
    async def validate_client(self, phone):
        """Return (True, None) only when the stored Telegram session is live and authorized.

        Reconnects/reloads the DB session when the in-memory client is missing, so
        purchase-time health checks also work after process restarts.
        """
        client = self.clients.get(phone)
        if client is None or not client.is_connected():
            try:
                acc = await self.accounts_col.find_one({"phone": phone})
            except Exception as e:
                return False, f"database lookup failed: {e}"
            if not acc or not acc.get("session_string"):
                return False, "session missing"
            state, detail = await self.ensure_connected(
                phone, session_str=acc["session_string"], max_attempts=self.interactive_attempts
            )
            if state != CONNECTED:
                return False, "session could not be opened" if state == INVALID_SESSION else (detail or state)
            client = self.clients.get(phone)
            if client is None:
                return False, "session could not be opened"

        try:
            if not await asyncio.wait_for(client.is_user_authorized(), self.connect_timeout):
                async with self._lock(phone):
                    await self._mark_invalid_locked(phone, "session is not authorized")
                return False, "session is not authorized"
            me = await asyncio.wait_for(client.get_me(), self.connect_timeout)
            if not me:
                return False, "account identity unavailable"
            return True, None
        except Exception as e:
            if classify_error(e) == "invalid":
                async with self._lock(phone):
                    await self._mark_invalid_locked(phone, f"{type(e).__name__}: {e}"[:200])
            return False, str(e) or type(e).__name__

    # ------------------------------------------------------- Request Again
    async def _ensure_ready(self, phone):
        """Live check for an interactive request. Never queues behind a running
        reconnect; ensure_connected() itself verifies authorization with Telegram."""
        return await self.ensure_connected(
            phone, max_attempts=self.interactive_attempts, blocking=False
        )

    async def request_again(self, user_id, phone):
        """Backs the existing "Request New OTP" button.

        Refreshes purchase validity + connection state and, only when the
        account is actually reachable, registers the pending OTP request.
        Returns a dict: ok, state, message (<=200 chars, fits a callback alert),
        expires_at, already_waiting.
        """
        doc = await self._find_doc(phone, sold_only=True)
        if not doc or str(doc.get("buyer_id")) != str(user_id):
            return _result(False, "not_found", "❌ No active purchase found for this number.")

        expires = self.expiry_of(doc)
        if self._is_expired(doc):
            if doc.get("access_expired") is not True:
                await self.expire_account(doc)
            when = f" on {expires.strftime('%d/%m/%Y %H:%M')} IST" if expires else ""
            return _result(False, EXPIRED,
                           f"⏰ The 72-hour validity of this number ended{when}. Please buy a new number.",
                           expires_at=expires)

        if not doc.get("session_string"):
            return _result(False, INVALID_SESSION,
                           "❌ This account has no usable session. Please contact support.",
                           expires_at=expires)

        status = self._cached_status(phone) or doc.get("conn_status")
        if status == INVALID_SESSION:
            return _result(False, INVALID_SESSION,
                           "❌ This account's session is no longer valid. Please contact support.",
                           expires_at=expires)

        retry_after = doc.get("conn_retry_after")
        if status == RATE_LIMITED and isinstance(retry_after, datetime) and retry_after > self._now():
            secs = int((retry_after - self._now()).total_seconds()) + 1
            return _result(False, RATE_LIMITED,
                           f"⏳ Telegram is rate-limiting this account. Please try again in about {secs}s.",
                           expires_at=expires)

        state, detail = await self._ensure_ready(phone)
        if state == CONNECTED:
            key = (user_id, phone)
            already = key in self.pending_requests
            self.pending_requests[key] = True
            msg = ("⏳ Already waiting for a new OTP. It will be sent automatically when it arrives."
                   if already else
                   "✅ Please wait for the new OTP. It will be sent automatically as soon as it arrives.")
            return _result(True, CONNECTED, msg, expires_at=expires, already_waiting=already)
        if state in (RECONNECTING, STANDBY):
            return _result(False, RECONNECTING,
                           "🔄 Reconnecting to this account. Please press the button again in a few seconds.",
                           expires_at=expires)
        if state == INVALID_SESSION:
            return _result(False, INVALID_SESSION,
                           "❌ This account's session is no longer valid. Please contact support.",
                           expires_at=expires)
        if state == RATE_LIMITED:
            return _result(False, RATE_LIMITED,
                           "⏳ Telegram is rate-limiting this account. Please try again in a minute.",
                           expires_at=expires)
        if state == EXPIRED:
            return _result(False, EXPIRED,
                           "⏰ The 72-hour validity of this number has ended. Please buy a new number.",
                           expires_at=expires)
        return _result(False, DISCONNECTED,
                       "⚠️ Could not reach Telegram for this account right now. Please try again shortly.",
                       expires_at=expires)

    # ------------------------------------------------ session-management API
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
            async with self._lock(phone):
                if self.clients.get(phone) is client:
                    self.clients.pop(phone, None)
        try:
            await self.accounts_col.update_one(
                {"phone": phone},
                {"$set": {"status": "logged_out"}}
            )
        except Exception as e:
            logging.error(f"Could not flag {phone} as logged_out: {e}")
        self._conn_state.pop(phone, None)
        await self._release_lease(phone)
        return True, "Bot's session logged out."

    async def logout_client(self, phone):
        """Terminate the Telethon client for this phone number."""
        async with self._lock(phone):
            if phone in self.clients:
                await self._remove_locked(phone)
                logging.info(f"Client for {phone} logged out by user request.")
            else:
                logging.warning(f"Attempt to logout non-existent client {phone}")

    async def remove_client(self, phone):
        async with self._lock(phone):
            await self._remove_locked(phone)

    async def stop_all(self):
        for attr in ("_supervisor", "_lease_task"):
            task = getattr(self, attr)
            setattr(self, attr, None)
            if task is not None and not task.done():
                task.cancel()
                try:
                    await task
                except (asyncio.CancelledError, Exception):
                    pass
        for phone in list(self.clients):
            async with self._lock(phone):
                await self._remove_locked(phone)
        self.clients.clear()
        # Hand over every lease immediately so a new deployment can take over
        # without waiting for the TTL.
        for phone in list(self._lease_until):
            await self._release_lease(phone)

    # ------------------------------------------------- restart / supervision
    def _should_restore(self, doc):
        status = doc.get("status")
        if status == "available":
            return True
        if status != "sold":
            return False
        if self._is_expired(doc) or not self._should_monitor_sold(doc):
            return False
        if doc.get("conn_status") == INVALID_SESSION:
            return False  # known-dead session: don't hammer Telegram on every boot
        ra = doc.get("conn_retry_after")
        if doc.get("conn_status") == RATE_LIMITED and isinstance(ra, datetime) and ra > self._now():
            return False
        return True

    async def load_all(self):
        """Restore OTP listeners from MongoDB after a restart/redeploy.

        Restores unsold stock (as before) AND purchased accounts that are still
        inside their 72h window. One failing account never blocks the others.
        """
        await self.backfill_expiry()
        docs = await self.accounts_col.find({
            "$or": [{"status": "available"},
                    {"status": "sold", "access_expired": {"$ne": True}}]
        }).to_list(length=None)

        # Carry persisted state into memory so transitions (e.g. invalid ->
        # admin notice) are not re-announced after every boot.
        for d in docs:
            if d.get("conn_status") in (INVALID_SESSION, RATE_LIMITED):
                self._conn_state[d["phone"]] = (d["conn_status"], d.get("conn_last_error"),
                                                d.get("conn_retry_after"))

        targets = [d for d in docs if d.get("session_string") and self._should_restore(d)]
        sem = asyncio.Semaphore(self.restore_concurrency)

        async def one(d):
            async with sem:
                try:
                    await self.ensure_connected(d["phone"], session_str=d["session_string"])
                except asyncio.CancelledError:
                    raise
                except Exception as e:
                    logging.error(f"❌ Unexpected error loading account {d.get('phone')}: {e}")

        await asyncio.gather(*(one(d) for d in targets))
        sold = sum(1 for d in targets if d.get("status") == "sold")
        logging.info(f"Restored {len(targets)} account client(s) ({sold} purchased).")

    async def reconnect_dropped(self):
        """Reconnect purchased accounts whose client went away. Skips accounts that are
        mid-reconnect, known invalid, rate limited, or in back-off cool-down."""
        now = self._now()
        count = 0
        async for d in self.accounts_col.find({"status": "sold", "access_expired": {"$ne": True}}):
            phone = d.get("phone")
            if not phone or self._is_expired(d) or not self._should_monitor_sold(d):
                continue
            if phone in self._standby:
                continue            # handled by retry_standby()
            client = self.clients.get(phone)
            if client is not None and client.is_connected():
                last = self._verified_at.get(phone)
                if last is None or (now - last).total_seconds() >= self.health_check_interval:
                    # Periodic live check catches zombie "connected" clients.
                    await self.ensure_connected(phone, blocking=False)
                continue
            status = self._cached_status(phone) or d.get("conn_status")
            if status == INVALID_SESSION:
                continue
            ra = d.get("conn_retry_after")
            if status == RATE_LIMITED and isinstance(ra, datetime) and ra > now:
                continue
            nb = self._retry_not_before.get(phone)
            if nb is not None and nb > now:
                continue
            state, _ = await self.ensure_connected(phone, blocking=False)
            if state == CONNECTED:
                count += 1
        return count

    async def run_maintenance(self):
        expired = await self.expire_due()
        await self.retry_standby()
        reconnected = await self.reconnect_dropped()
        return expired, reconnected

    async def _supervisor_loop(self):
        while True:
            try:
                await self.run_maintenance()
            except asyncio.CancelledError:
                raise
            except Exception as e:
                logging.error(f"Account supervisor cycle failed: {e}")
            await self._sleep(self.supervisor_interval)

    async def _lease_loop(self):
        # Separate from the supervisor so slow reconnects can never starve renewals.
        while True:
            try:
                await self.renew_leases()
            except asyncio.CancelledError:
                raise
            except Exception as e:
                logging.error(f"Lease heartbeat cycle failed: {e}")
            await self._sleep(self.lease_heartbeat)

    def start_supervisor(self):
        """Start the background tasks (idempotent: never spawns a second set)."""
        if self._lease_task is None or self._lease_task.done():
            self._lease_task = asyncio.create_task(self._lease_loop(), name="account-lease-heartbeat")
        if self._supervisor is not None and not self._supervisor.done():
            return self._supervisor
        self._supervisor = asyncio.create_task(self._supervisor_loop(), name="account-supervisor")
        return self._supervisor
