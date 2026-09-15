
Redeploy sold-status fix:
- Invalid session + status=available -> inactive, with Replace/Remove buttons.
- Invalid session + status=sold -> stays sold.
- Already inactive/other historical records keep their current status.

Sold invalid-session no-repeat fix:
- Sold records stay status=sold.
- If a sold session is invalid on startup, session_string is removed and monitor_disabled=true is stored.
- load_all() excludes those sold records on future restarts.
- Therefore the same sold invalid account is not rechecked/notified on every restart.
