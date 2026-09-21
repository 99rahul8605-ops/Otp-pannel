# OTP Panel - Updated Build

This package contains the latest cumulative bot updates.

## Important

- Main entry point: `bot.py`
- Existing private/admin logs are preserved.
- Public masked logs are supported through `PUBLIC_LOG_CHANNEL_ID`.
- Clone/Franchise security, Margin Wallet, Margin-to-Master transfer, and 2-step margin withdrawal confirmation are included.
- Before production deployment, test payment, clone, purchase, deposit, and withdrawal flows on a small test clone.

## Clone Fraud Protection Notes

CLONE / FRANCHISE FRAUD PROTECTION PATCH

Default mode: MASTER MANAGED
- Master/platform handles clone customer deposits.
- Clone owner cannot manually add customer balance.
- Successful sale margin is collected in clone earnings_wallet.
- Clone owner can request margin withdrawal.

OWN PAYMENT mode
- Clone owner must first lock the required security deposit from their personal master-bot balance.
- Security remains locked and is not automatically returned on clone removal.
- Every clone-customer deposit additionally locks the SAME amount from the clone owner's free master-bot balance.
- If free backing is insufficient, that deposit is rejected.

Example:
Owner master balance: Rs 2,000
Security requirement: Rs 1,000
After switching to OWN PAYMENT:
  Security locked: Rs 1,000
  Free balance: Rs 1,000

Customer deposits Rs 1,000:
  Customer backing locked: Rs 1,000
  Owner free balance: Rs 0

So the owner cannot accept the customer's deposit and then spend its backing on the master bot.

Other protections:
- approved own-payment deposits become customer_funds_locked / owner_backed_balance
- cancelled/rejected/expired reservations release backing
- sale failures roll back finance reservations
- clone referral rewards are funded by clone, not master
- manual customer balance adjustment is blocked for clone admin
- low security falls back to master-managed protection
- clone closure notifies users and freezes deposits
- clones with liabilities enter CLOSING instead of being erased
- historical clone/users/deposits/orders/audit records remain available to master
- master admin can inspect clone users, balances, backing, deposits, orders and finance audit
- security deposit stays locked on removal

Environment fallback:
CLONE_SECURITY_DEPOSIT_MIN=1000

Static validation:
bot.py: PASS
account_manager.py: PASS
vnh_server.py: PASS
audit_referrals.py: PASS

No live Telegram/Razorpay/MongoDB integration test was performed.

---

## Margin Wallet Update

MARGIN WALLET UPDATE

Added for clone owner/admin only:

Main Menu:
- 💵 Margin Wallet
- 🏢 Franchise Finance

Margin Wallet shows:
- Available Margin
- Total Margin Earned
- Master Bot Wallet balance

Actions:
1. 💸 Withdraw Margin
   - Existing admin-approved withdrawal request flow.

2. 💰 Transfer to Wallet
   - Clone owner enters amount.
   - Amount is atomically reserved/deducted from earnings_wallet.
   - Same amount is instantly credited to owner's MASTER-bot personal balance.
   - No withdrawal approval required.
   - Finance audit event is written.
   - If the master-wallet credit throws an error, Margin Wallet deduction is rolled back.

The button is shown only when:
- current bot is a franchise/clone, AND
- current user is that clone's owner/admin.

Existing clone fraud/security protections remain intact.

Syntax:
- bot.py PASS
- account_manager.py PASS
- vnh_server.py PASS

---

## Margin Withdraw Payment Confirm

CLONE MARGIN WITHDRAWAL - PAYMENT CONFIRMATION UPDATE

New flow:

1. Clone owner requests Margin Withdrawal
   -> amount is reserved from Margin Wallet
   -> request status = pending

2. Master admin presses "Approve"
   -> request status = payout_pending
   -> withdrawal is NOT finalized yet
   -> admin sees:
      [✅ Payment Sent]
      [❌ Cancel & Restore Margin]

3. Admin actually sends the external payment.

4. Admin presses "Payment Sent"
   -> request status = approved
   -> payment_sent_at / payment_sent_by saved
   -> clone owner gets "Margin Withdrawal Paid" message
   -> audit event saved
   -> admin message becomes PAID / COMPLETED

If admin changes mind before sending payment:
- "Cancel & Restore Margin" restores the reserved amount exactly once.

Initial Reject:
- Reject still restores Margin Wallet exactly once.

This prevents an accidental Approve click from closing a withdrawal before
the real external payment has actually been sent.

Syntax checks:
- bot.py PASS
- account_manager.py PASS
- vnh_server.py PASS

---

## Patch Notes Latest Cumulative

Original/full bot cumulative patch

Base:
Otp-pannel_ORIGINAL_FULL_SOLD_INVALID_NO_REPEAT.zip

Added:
1. Support URL safety
   - Invalid stored support links no longer crash /start.
   - @username and t.me/username normalize to https://t.me/username.
   - Invalid admin input is rejected.

2. Telegram callback/transient RPC resilience
   - 244 explicit event.answer() calls now use safe_callback_answer().
   - QueryIdInvalidError from expired callbacks is ignored safely.
   - Temporary Telegram ServerError/RPC -500 errors retry on force-join checks.
   - Asyncio background callback/server errors are handled more cleanly.

3. Referral reward one-time atomic claim
   - Prevents duplicate reward from concurrent manual/Razorpay approvals.
   - First approved deposit consumes the reward opportunity exactly once.
   - Adds referral audit metadata.
   - Historical duplicates are NOT automatically reversed.

Preserved original/full features:
- Server 1
- Server 2 / VNH
- clone / franchise
- Razorpay / auto-payment
- SMM
- inactive Replace/Remove
- stock Telegram names
- OTP received time
- sold status preservation
- sold invalid no-repeat cleanup

---

## Public Log Channel Update

PUBLIC LOG CHANNEL UPDATE

Add to .env:
PUBLIC_LOG_CHANNEL_ID=-1001234567890

The master bot must be able to post in that public channel.

Public events added:
1. Account Sale / Purchase
   - Server 1 and Server 2
   - masked Telegram user ID
   - masked phone number
   - country
   - retail amount
   - timestamp
   - "Thank you for your purchase!"
   - source bot username footer

2. Deposit Approved
   - masked Telegram user ID
   - amount
   - approved status
   - timestamp
   - source bot username footer

3. Stock Added
   - country
   - price
   - quantity added
   - timestamp
   - source bot username footer

Privacy:
- Public logs do NOT expose full phone numbers.
- Public logs do NOT expose full Telegram user IDs.
- Supplier/wholesale cost is NOT shown in the public sale log.
- Deposit transaction/reference IDs are NOT shown publicly.
- Existing private/admin logs stay unchanged and still contain their original details.

Source bot footer:
- Username is fetched live from Telegram using get_me() for the current master/clone.
- The public log itself is sent through the master bot to the configured public channel,
  so clone bots do not each need channel admin permission.

Syntax checks:
- bot.py PASS
- account_manager.py PASS
- vnh_server.py PASS

---

## Readme

Redeploy sold-status fix:
- Invalid session + status=available -> inactive, with Replace/Remove buttons.
- Invalid session + status=sold -> stays sold.
- Already inactive/other historical records keep their current status.

Sold invalid-session no-repeat fix:
- Sold records stay status=sold.
- If a sold session is invalid on startup, session_string is removed and monitor_disabled=true is stored.
- load_all() excludes those sold records on future restarts.
- Therefore the same sold invalid account is not rechecked/notified on every restart.

---

## Readme Feature Update

Original full bot feature update

Added without removing existing Server 2/VNH, clone/franchise, Razorpay, SMM or other original features:
1. Inactive Server 1 stock notifications now include Replace Session and Remove From Stock buttons.
2. Replace validates a fresh Telethon StringSession and returns the same record to available stock.
3. Stock/admin account list shows Telegram profile name. New single/bulk additions save name/user-id/username; older loaded sessions are backfilled when viewed.
4. Buyer OTP message shows exact Telegram-arrival date/time in IST. Admin OTP notification uses the same timestamp. last_otp_received_at is stored.
5. Startup invalid-session notifications also include the quick-action buttons when the DB account can be resolved.

Syntax checked: bot.py, account_manager.py, vnh_server.py.

## Public Log Channel Setup

Add this to `.env`:

```env
PUBLIC_LOG_CHANNEL_ID=-1001234567890
```

The master bot must be able to post in that channel.

Public logs currently include:
- Stock added
- Deposit approved
- Server 1 purchase
- Server 2 purchase

Sensitive details such as full Telegram user IDs, full phone numbers, wholesale/supplier cost, and deposit references are not shown publicly.

## Validation

Latest package syntax-check status:
- `bot.py` — PASS
- `account_manager.py` — PASS
- `vnh_server.py` — PASS


## Force-Join Manager Update

Force-Join channels/groups are now managed individually.

- `Add Channel / Group` appends one new entry.
- Existing IDs/usernames remain saved automatically.
- You no longer need to re-enter old channel/group IDs when adding another.
- `Remove One` shows buttons for existing entries and removes only the selected one.
- `Clear All` is still available.
- Duplicate entries are prevented.
- New entries are checked with Telegram before being saved.


## Public Log Formatting Fix

Fixed literal `\\n\\n` showing before the bot username. Public logs now use real line breaks.
