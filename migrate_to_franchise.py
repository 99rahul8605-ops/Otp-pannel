"""
ONE-TIME MIGRATION — run this ONCE, on the MASTER bot's database, BEFORE
deploying the franchise-isolation update.

What it does: tags every existing document in users/deposits/withdrawals/
orders/smm_orders/settings that doesn't already have a franchise_id with
franchise_id="master" — so all your current real users, balances, and
history stay exactly where they are and remain visible to the master bot.

Safe to run more than once (only touches documents missing the field).
Does NOT touch accounts_col (shared stock), franchise_wallets_col, or
bot_clones_col — those are intentionally global already.

Usage:
    pip install motor python-dotenv --break-system-packages
    python3 migrate_to_franchise.py
"""
import asyncio
import os
from dotenv import load_dotenv
from motor.motor_asyncio import AsyncIOMotorClient

load_dotenv()

MONGO_URL = os.getenv("MONGO_URL", "mongodb://localhost:27017").strip()
DB_NAME = "otp_bot"
COLLECTIONS = ["users", "orders", "deposits", "settings", "withdrawals", "smm_orders"]


async def main():
    client = AsyncIOMotorClient(MONGO_URL)
    db = client[DB_NAME]

    print(f"Connecting to {MONGO_URL} / db '{DB_NAME}'...")
    total = 0
    for name in COLLECTIONS:
        col = db[name]
        result = await col.update_many(
            {"franchise_id": {"$exists": False}},
            {"$set": {"franchise_id": "master"}}
        )
        print(f"  {name}: tagged {result.modified_count} existing document(s) as franchise_id='master'")
        total += result.modified_count

    print(f"\nDone. {total} documents migrated. Your existing users/balances/history "
          f"are now scoped to the master bot and will show up exactly as before.")
    client.close()


if __name__ == "__main__":
    asyncio.run(main())
