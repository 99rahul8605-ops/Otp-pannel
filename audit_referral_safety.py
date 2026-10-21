"""Read-only pre-deployment audit for unique wallets and old referral claims.

Run from the project directory with .env configured:
    python audit_referral_safety.py

Never changes customer balances or the database schema. Review the flagged
records before deploying the hardened version.
"""
import asyncio
import os
from motor.motor_asyncio import AsyncIOMotorClient
from dotenv import load_dotenv

load_dotenv()

async def main():
    client = AsyncIOMotorClient(os.getenv('MONGO_URL', 'mongodb://localhost:27017'), serverSelectionTimeoutMS=10000)
    try:
        db = client['otp_bot']
        await client.admin.command('ping')
        print('1) Duplicate user-wallet documents (must fix BEFORE rollout):')
        duplicates = await db['users'].aggregate([
            {'$group': {'_id': {'franchise_id': '$franchise_id', 'user_id': '$user_id'},
                        'count': {'$sum': 1}, 'document_ids': {'$push': '$_id'}}},
            {'$match': {'count': {'$gt': 1}}},
            {'$limit': 50},
        ]).to_list(length=50)
        if duplicates:
            for d in duplicates:
                print(f"  scope/user={d['_id']} count={d['count']} docs={d['document_ids']}")
            print('  WARNING: Startup refuses to run if duplicate scoped wallet records remain.')
        else:
            print('  None found.')

        print('\n2) Unfinished claims from older versions (do NOT auto-retry):')
        legacy = await db['referral_signup_claims'].find({
            'status': {'$ne': 'paid'},
            'schema_version': {'$ne': 2},
        }, {'_id': 1, 'status': 1, 'referrer_id': 1, 'referred_user_id': 1}).limit(50).to_list(length=50)
        if legacy:
            for c in legacy:
                print('  ', c)
            print('  Review these against actual balances/funding records manually.')
        else:
            print('  None found.')

        print('\n3) New-version claims requiring review:')
        pending = await db['referral_signup_claims'].find({
            'schema_version': 2,
            'status': 'credit_error_review_required',
        }, {'_id': 1, 'status': 1, 'error': 1}).limit(50).to_list(length=50)
        if pending:
            for c in pending:
                print('  ', c)
            print('  These can generally be recovered via verified /start or Check Again.')
        else:
            print('  None found.')
    finally:
        client.close()


if __name__ == '__main__':
    asyncio.run(main())
