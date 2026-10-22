# Razorpay without webhook — API verification

## Bot setup

1. Keep your existing production `.env` when upgrading. Back up both MongoDB
   and bot source, and stop all instances of this bot before replacing code.
2. In Telegram open **Admin Panel → Finance & Transactions → Razorpay Settings**
   and configure the **Key ID** and **Key Secret** for the intended Razorpay
   account (test keys for testing; live keys for real money). You may also set
   `RAZORPAY_KEY_ID` and `RAZORPAY_KEY_SECRET` in `.env` for the master scope.
3. Do **not** configure Razorpay Webhooks to use this mode. A webhook secret,
   domain, SSL certificate, open port and callback URL are not required.
4. Admin can turn **Manual Payment OFF** in Payment Options, while **Razorpay QR**
   and **Razorpay Payment Link** remain offered with their own buttons.
5. Restart normally and test both payment types with Razorpay TEST keys and
   simulated TEST payments **before** switching to live keys.

Environment configuration (optional):

```ini
RAZORPAY_ENABLE_WEBHOOK=false
RAZORPAY_API_POLL_SECONDS=60
# RAZORPAY_KEY_ID=rzp_test_...   # or configure in Admin Panel
# RAZORPAY_KEY_SECRET=...      # never disclose or commit secret
```

## Customer flow

- User selects **Razorpay QR** or **Razorpay Payment Link**, created with a
  unique deposit reference and fixed payment amount.
- The bot checks Razorpay through Basic Auth using server-side API keys via:
  - `GET /v1/payments/qr_codes/{qr_id}/payments` for QR transactions.
  - `GET /v1/payment_links/{plink_id}` for Payment Links.
  - `GET /v1/payments/{payment_id}` to confirm final captured status.
- Every new deposit requires a matching currency `INR`, amount in paise,
  captured payment state, and corresponding QR/Link reference. Payment Link
  verification also requires the deposit-specific `reference_id`, full paid
  amount and a matching captured payment record.
- The bot checks automatically every 60 seconds by default (batch limit 50),
  and the user can press **✅ Check Payment**. Payment gateway delays may
  require another check; clicking is not proof of payment.
- A MongoDB unique index on `razorpay_verified_payment_id` and an atomic
  `wallet_deposit_receipts` wallet entry help prevent double credit on
  simultaneous checks and retries. No index may be dropped to bypass errors.
- Successful deposit / first-deposit referral logs still use the existing
  private `LOGS_CHANNEL_ID` configuration.

## Limitations and operations

- Manual static QR and custom HTTPS links are **not** verified by Razorpay API;
  they still need screenshots and an authorized admin approval. Turning Manual
  OFF hides both of those paths.
- Auto-approval needs an enabled Razorpay account, correct permissions and
  outbound HTTPS access to `api.razorpay.com` on port 443.
- Because no webhook is configured, payment recognition is *polled*, not
  instantaneous. The bot process must be running to poll.
- Only pending Razorpay deposits created in the past 7 days are automatically
  polled; successful approvals with unfinished posting are also retried. A
  customer can use **Check Payment** on an older pending deposit, or contact
  support. If the bot is offline for longer periods, reconcile manually.
- Pending Razorpay deposits cannot be cancelled from the Telegram button
  while funds may be in flight. Investigate old pending deposits from the
  Razorpay Dashboard before refunding or releasing any reserved capacity.
- Reconciliation of interruptions between collateral locking and downstream
  bookkeeping is not a substitute for a full financial ledger. For a live
  production deployment, use MongoDB transactions/ledger reconciliation and
  monitor/log pending approved deposits.
- An API key's secret must remain on the server or in protected admin settings.
  Never paste it into user chats or public logs.
