Original full bot feature update

Added without removing existing Server 2/VNH, clone/franchise, Razorpay, SMM or other original features:
1. Inactive Server 1 stock notifications now include Replace Session and Remove From Stock buttons.
2. Replace validates a fresh Telethon StringSession and returns the same record to available stock.
3. Stock/admin account list shows Telegram profile name. New single/bulk additions save name/user-id/username; older loaded sessions are backfilled when viewed.
4. Buyer OTP message shows exact Telegram-arrival date/time in IST. Admin OTP notification uses the same timestamp. last_otp_received_at is stored.
5. Startup invalid-session notifications also include the quick-action buttons when the DB account can be resolved.

Syntax checked: bot.py, account_manager.py, vnh_server.py.
