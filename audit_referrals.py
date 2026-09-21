import os
import asyncio
from motor.motor_asyncio import AsyncIOMotorClient
from dotenv import load_dotenv

load_dotenv()

MONGO_URL = os.getenv("MONGO_URL", "mongodb://localhost:27017")

async def main():
    db = AsyncIOMotorClient(MONGO_URL)["otp_bot"]
    users = db.users

    count = 0
    print("Users marked referral_bonus_paid=True:")
    async for u in users.find(
        {"referral_bonus_paid": True},
        {
            "_id": 0,
            "user_id": 1,
            "referred_by": 1,
            "scope_id": 1,
            "referral_bonus_status": 1,
            "referral_bonus_amount": 1,
            "referral_bonus_first_deposit_amount": 1,
            "referral_bonus_paid_at": 1,
        }
    ):
        print(u)
        count += 1

    print(f"\\nTotal: {count}")
    print("Legacy rows from before this patch may not contain status/amount metadata.")
    print("Historical duplicate rewards are not auto-reversed.")

asyncio.run(main())
