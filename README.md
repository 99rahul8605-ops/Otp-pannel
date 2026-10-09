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
