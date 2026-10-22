# Payment Options — Admin Setup

This update adds an ON/OFF switch for **manual (screenshot-reviewed)** payments, a configurable HTTPS custom payment link, and an explicit Razorpay Payment Link button while retaining QR payments.

## Admin menu

`Admin Panel → Finance & Transactions → Payment Options`

- **Turn Manual OFF / ON**: Defaults to ON for backward compatibility. Switching OFF hides and blocks new screenshot payments (both UPI QR and custom payment link). The toggle is persisted in MongoDB and is effective without restarting the bot. Existing `awaiting_proof` or `pending` manual deposits remain actionable/reviewable, so users who already paid are not stranded.
- **Set / Change Payment Link**: Set a single HTTPS link, or send `REMOVE` to clear it. This is an administrator-owned **static link**, not a dynamic amount-specific charge. It does **not** auto-credit; the user must send payment proof and an admin must verify payment. Verify that any configured link is trustworthy and can receive the correct amount before enabling deposits.
- **UPI ID / QR Settings**: Existing UPI ID and QR remain unchanged. The QR method is shown only when Manual is ON and a UPI ID is set. The manual QR message also includes a button for the custom payment link, if one is set.
- **Razorpay Settings**: When Razorpay keys are set, the user can choose either `Razorpay QR (Auto)` or `Razorpay Payment Link (Auto)`; both are verified using Razorpay API keys without requiring webhooks. Background polling plus a ✅ Check Payment button trigger verification. Manual OFF does not disable these choices. The existing Razorpay QR path also retains automatic fallback to a link if generating/sending QR fails.

Payment settings are shared within the **payment credential scope**: master-managed clones use master payment settings; own-payment clones can change their own settings.

## Customer deposit menu

Depending on settings, the following buttons will be displayed:

- ⚡ Razorpay QR (Auto) — only with Razorpay credentials.
- 🔗 Razorpay Payment Link (Auto) — only with Razorpay credentials.
- 📲 UPI QR + Screenshot — only with Manual ON and UPI ID configured.
- 🔗 Payment Link + Screenshot — only with Manual ON and custom HTTPS link configured.

With Manual OFF and no Razorpay configured, deposits are temporarily unavailable by design. Manual payment requests already underway can still be completed/cancelled and approved/rejected.

## Installation / checks

1. Back up your bot source and MongoDB first.
2. Stop the bot before deploying the new files; keep your existing `.env` and database.
3. Run `python3 audit_referral_safety.py` (existing wallet audit) and `python3 -m unittest discover -s tests -v` (offline tests).
4. Restart with your normal bot command.
5. Test manual ON/OFF, Razorpay link and QR with Razorpay TEST keys, pending checks, and repeated Check Payment clicks. See `RAZORPAY_NO_WEBHOOK.md`.

New settings keys (per credential scope) in `otp_bot.settings`: `manual_payment_enabled` (boolean) and `manual_payment_link` (string). Neither requires a manual DB migration. A payment link is **not** a Razorpay API key; its presence never enables auto-credit.

Razorpay gateway polling is **webhook-free by default** (`RAZORPAY_ENABLE_WEBHOOK=false`). It uses Razorpay API Key ID and Secret and requires no domain. The optional historical webhook may still be enabled explicitly.
