# OTP Bot — latest source with Server 3

This package contains the current OTP bot source plus the new Heart TG Store integration as **Server 3**.

## Files

- `bot.py` — main Telegram bot
- `account_manager.py` — existing account/session manager used by `bot.py`
- `server2_server.py` — existing Server 2 integration
- `server3_server.py` — new Server 3 integration
- `requirements.txt`
- `.env.example`

## Server 3 naming

The external API exposes two upstream pools. The bot never shows the supplier's internal names to customers:

- supplier `server: 1` / Main Server → **Provider 1**
- supplier `server: 2` → **Provider 2**

Top-level customer flow is therefore:

`Buy Account → Server 3 → Provider 1 / Provider 2`

## Environment

```env
SERVER3_API_KEY=your_api_key
SERVER3_API_BASE=https://hearttgstoreapi.duckdns.org/api
SERVER3_MARKUP_PERCENT=20
```

Authentication is sent as `X-API-Key`.

`SERVER3_MARKUP_PERCENT` is this bot's own markup on the supplier's actual cost. Supplier fields such as `sell_price`, `your_price`, `margin`, and `your_inr` are intentionally ignored for customer pricing.

The markup can also be changed live from:

`Admin → Accounts & Stock → Set Server 3 Markup`

The setting is scoped per master/franchise bot, the same way the existing settings collection is scoped.

## Provider 1

Useful API flow implemented:

1. `GET /stock?server=1` — account categories, real stock/counts.
2. `POST /buy/account` with `test_mode:true` — current supplier cost preview.
3. `POST /buy/account` — quote + 120-second `confirm_token`.
4. Second `POST /buy/account` with `confirm_token` — purchase.
5. `POST /order/otp` — starts the OTP window.
6. `GET /order/status` — polled at 5+ second intervals.

Only OTP delivery is exposed to customers. Session-only categories are filtered out.

The bot does **not** automatically fetch `/order/session`, because fetching a waiting Provider 1 session can mark the order delivered and remove the supplier's automatic refund eligibility.

### Provider 1 refund handling

The first OTP window is supplier-refundable. The bot continues monitoring a pending order in the background so a supplier `refunded` status is mirrored back to the customer's bot wallet even if the customer does not press another button.

A confirmed supplier refund also reverses the corresponding franchise finance/margin entry exactly once.

A second/new OTP request is never opened silently. If the first listener/window ends, the customer gets a `Request New OTP` button.

## Provider 2

Useful API flow implemented:

1. `GET /stock?server=2` — country list only.
2. `POST /buy/server2` with `delivery:"otp", test_mode:true` — live cheapest-price preview for the selected country.
3. Actual `POST /buy/server2` with the previewed exact `price` and `delivery:"otp"` — avoids silently jumping to a more expensive tier.
4. `POST /order/otp` + `GET /order/status` — OTP retrieval.

The bot does not call the per-country price-list endpoint for all 197 countries. That avoids wasting the supplier's limited price-check quota. A live price is fetched only after the customer selects a country.

## Error safety

Supplier failures are branched using the machine `code`, not human `error` text.

Important behaviors:

- `success:false` purchase responses → customer reservation is rolled back/refunded; supplier docs state these are not charged.
- `PRICE_CHANGED`, `PRICE_UNAVAILABLE`, `STOCK_TAKEN` → no automatic higher-price purchase.
- `SERVER_BUSY`, rate/listener limits → retry timing is respected; no tight loop.
- `PURCHASE_UNCERTAIN` → never auto-retried.
- `ORDER_REFUNDED` / status `refunded` → customer wallet refund is applied once and franchise finance is reversed once.
- a supplier **success** response missing phone/order details is treated as an admin-review case rather than automatically refunding the customer, because a successful supplier purchase may already have been charged.

## Existing Server 2 compatibility

The previous Server 2 integration remains unchanged. Preferred env names remain:

```env
SERVER2_API_KEY=...
SERVER2_API_BASE=https://api.vnhotp.com
SERVER2_MARKUP_PERCENT=20
```

Legacy VNH env fallbacks remain in `bot.py` for older deployments.


## Server 1 Bulk Buy

Server 1 price confirmation now includes **Bulk Buy** with two delivery modes:

- **Direct OTP — One by One:** the first number is sent immediately; after its first OTP is delivered, the next number is sent automatically until the batch completes.
- **Session ZIP + 2FA:** all selected accounts are packaged into one ZIP as Telethon SQLite `.session` files plus `accounts.txt` mapping each phone to its 2FA password.

Bulk quantity defaults to a maximum of 20 and can be configured with `BULK_BUY_MAX_QTY`. Stock is FIFO and every selected session is live-validated before checkout.


## Server 1 bulk Session ZIP handoff

After a Session ZIP is successfully delivered to the buyer, the main bot now:

- disconnects its live OTP-monitoring client for every transferred number;
- removes `session_string` from the main MongoDB account record;
- keeps the account/order record as sold for history and finance;
- marks the account with `session_transferred`, `session_transferred_at`, and `session_removed_from_main`;
- does **not** call Telegram logout/revoke, so the delivered `.session` file remains valid.

If ZIP delivery fails, this cleanup is not run and the existing refund/stock-release flow remains intact.


## Stock announcement deep links

Every successful Server 1 stock addition now posts a public stock announcement with:

- country;
- newly added quantity;
- current available quantity at that exact stock price;
- the current customer selling price after account markup;
- a **Buy This Stock** URL button.

The button uses a short Mongo-backed Telegram `/start` token, so country names with spaces or symbols are supported. Clicking it opens that exact Server 1 country + price row directly in the bot and shows **Buy 1 Account** / **Bulk Buy** when stock is available. If that row has sold out, the user is sent to the latest stock browser instead.

Set a dedicated channel with:

```env
ANNOUNCEMENT_CHANNEL_ID=-1001234567890
```

If it is left blank, stock announcements use `PUBLIC_LOG_CHANNEL_ID`. The posting bot must be able to send messages/buttons in that channel.


## Manual payment review group

Set `PAYMENT_GROUP_ID=-1001234567890` to send every submitted manual UPI screenshot to one central Telegram group/channel with Approve/Reject buttons. If it is blank, the bot falls back to `LOGS_CHANNEL_ID`. The master bot must be able to post messages and inline buttons there.

## Referral rewards

When the referral program is enabled, a **new user** who registers through an
existing user's `?start=ref<user_id>` link and joins **every required
channel/group** earns the inviter a **fixed ₹0.05 join reward**. The bot checks
membership both on `/start` and when the new user taps **✅ Check Again**;
a failed/incomplete membership check never pays the reward. This is awarded just once
per eligible referred user; reopening `/start` does not pay it again. Existing
users are not retroactively awarded a join bonus. On that referred user's
**first approved deposit**, the existing percentage reward is paid separately
(default **10%**, capped at **₹5** unless changed in Referral Bonus Settings).
Both rewards are credited to the inviter's balance, withdrawable balance and
referral earnings; in franchise clones, both use the existing clone-funded
referral mechanism.

### Private referral logs

Set `LOGS_CHANNEL_ID=-1001234567890` in your `.env` to receive private
**Referral Join Bonus Credited** logs (referrer/referred Telegram IDs,
usernames, verified required-channel count, ₹0.05, timestamp) and **Referral
First Deposit Bonus Credited** logs (users, deposit amount, bonus rate/cap,
credited amount, timestamp). Only the **master bot** sends to this channel;
add it as an admin with permission to post. These records are **not** sent
to `PUBLIC_LOG_CHANNEL_ID`. The private channel must already exist and the
ID must be real, not the example value. If `LOGS_CHANNEL_ID` is omitted or
inaccessible, credits work but private Telegram logs cannot be delivered.

Required channels/groups can be configured via the bot's force-join settings,
or for the master bot with `FORCE_JOIN_CHAT_ID` / comma-separated
`FORCE_JOIN_CHAT_IDS`. The bot must be able to verify membership in each
required channel/group; missing access prevents a join reward. **If no
required chats are configured, the join reward is paid at registration**
(because there are no mandatory chats to check).

## Verified-join referral payments: hardened deployment

- The fixed **₹0.05** bonus is issued only for a *new, valid referral* **after
  verifying all current mandatory joins**. Both `/start` and **Check Again**
  run this check. It is separate from the **10% of first approved deposit,
  capped at ₹5**, which is retained.
- Both rewards use deterministic idempotency receipts embedded atomically
  alongside wallet `$inc`. Clone funding and own-payment backing also use
  deterministic receipts. Concurrent callbacks, retried writes, and ambiguous
  network responses cannot apply the same new reward a second time.
- Existing private `LOGS_CHANNEL_ID` receives referrer/referee IDs and bonus
  details via the existing `log_event` function. No new channel variable.
  The master bot must already have permission to post there. Telegram logging
  is best-effort and is separate from the financial credit transaction.
- The bot creates a **unique index** on `(franchise_id, user_id)` on startup
  to prevent two wallet records for the same person and scope. **If duplicate
  records already exist, startup fails safely**. Back up MongoDB and run
  `python audit_referral_safety.py` before deployment; reconcile duplicate
  user documents manually before restarting.
- Pre-upgrade, non-`paid` join claims have no atomic funding/credit receipt.
  The code intentionally refuses to replay them, to avoid paying twice.
  Run `python audit_referral_safety.py` to identify claims requiring review.
- The fixed referral credit and clone backing are once-per-reward operations,
  not a multi-document MongoDB transaction. If a worker crashes during the
  sequence, a verified user can issue `/start` or **Check Again** to resume.
  Do not manually alter receipts unless you have reconciled ledgers.
- Always test on a **database backup/staging clone**, including master and
  franchise own-payment modes, before replacing the live process.

Offline regression tests:

```bash
python -m unittest discover -s tests -v
```


## Admin-editable referral join reward

Open `Admin Panel → Finance & Transactions → Referral Bonus Settings → Edit Join Bonus (₹)`
and send an amount such as `0.05`, `0.50` or `1`. The value is saved per bot
(master or each franchise clone) in MongoDB and takes effect immediately for
**new join reward claims**. Allowed range is ₹0–₹1000, with up to two decimal
places. The default is ₹0.05; `REFERRAL_SIGNUP_BONUS=0.05` optionally sets
that starting value for bots without an override.

Referrals earn the join reward only after **all required channels/groups are
verified**. A claim's amount is snapshotted when first created: retrying
a pending claim after an admin changes the rate uses the **original** amount
for funding, wallet credit, member notification and the existing private
`LOGS_CHANNEL_ID` log. Previously paid referrals are not recalculated.

The **first approved deposit bonus** is still independently configurable
(10% by default, with ₹5 cap). To disable the join amount while keeping the
first-deposit reward, set the join amount to ₹0. To turn off the whole referral
program use the ON/OFF switch.

For an existing database, run `python audit_referral_safety.py` before deploying;
old incomplete reward claims may need manual review. Test locally with
`python -m unittest discover -s tests -v`. These are offline tests, not a
live MongoDB/Telegram deployment verification.
