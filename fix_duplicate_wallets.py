#!/usr/bin/env python3
"""Conservative one-time repair for two verified, empty duplicate master wallets.

Read-only preview by default. --apply creates a local JSON backup before deleting
and building the uniqueness index. STOP all bot/clone writers before running.
"""
import os
import sys
from datetime import datetime, timezone
from pathlib import Path
from bson.json_util import dumps
from dotenv import load_dotenv
from pymongo import ASCENDING, MongoClient

EXPECTED = {8601299473: 27, 1041152821: 4}
FIELDS = {
    '_id', 'franchise_id', 'user_id', 'balance',
    'withdrawable_balance', 'referred_by', 'referral_bonus_paid', 'joined_at',
}


def main():
    if len(sys.argv) > 2 or (len(sys.argv) == 2 and sys.argv[1] != '--apply'):
        raise SystemExit('Usage: python3 fix_duplicate_wallets.py [--apply]')
    apply = '--apply' in sys.argv
    load_dotenv(dotenv_path=str(Path.cwd() / '.env'))
    url = os.getenv('MONGO_URL', 'mongodb://localhost:27017').strip()
    client = MongoClient(url, serverSelectionTimeoutMS=10000)
    try:
        client.admin.command('ping')
        col = client['otp_bot']['users']
        groups = list(col.aggregate([
            {'$group': {'_id': {'franchise_id': '$franchise_id', 'user_id': '$user_id'}, 'count': {'$sum': 1}}},
            {'$match': {'count': {'$gt': 1}}},
        ]))
        actual = {(g['_id'].get('franchise_id'), g['_id'].get('user_id')): g['count'] for g in groups}
        expected = {('master', user_id): count for user_id, count in EXPECTED.items()}
        if actual != expected:
            raise SystemExit(f'STOP: Duplicate groups changed. Found {actual}. No changes made.')

        to_delete, to_backup = [], []
        for uid, count in EXPECTED.items():
            docs = list(col.find({'franchise_id': 'master', 'user_id': uid}))
            if len(docs) != count:
                raise SystemExit(f'STOP: Count changed for {uid}; no changes made.')
            for doc in docs:
                if (set(doc) != FIELDS or doc['balance'] != 0
                        or doc['withdrawable_balance'] != 0
                        or doc['referred_by'] is not None
                        or doc['referral_bonus_paid'] is not False):
                    raise SystemExit(f'STOP: Unexpected wallet data in {uid}/{doc["_id"]}; no changes made.')
            docs.sort(key=lambda d: (d['joined_at'], str(d['_id'])))
            keep = docs[0]
            extras = docs[1:]
            to_backup.extend(docs)
            to_delete.extend(extras)
            print(f'User {uid}: keep _id={keep["_id"]}; delete {len(extras)} empty duplicates')

        print(f'Total to keep: {len(EXPECTED)}. Total to delete: {len(to_delete)}.')
        if not apply:
            print('READ-ONLY preview complete. To continue after database backup and stopping all bots:')
            print('  python3 fix_duplicate_wallets.py --apply')
            return

        now = datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ')
        backup = Path.cwd() / f'users_duplicate_backup_{now}.json'
        with backup.open('x', encoding='utf8') as f:
            os.chmod(backup, 0o600)
            f.write(dumps({'collection': 'otp_bot.users', 'documents': to_backup}, indent=2))
            f.flush()
            os.fsync(f.fileno())
        print(f'Backup saved (31 BSON-safe JSON documents): {backup}')
        confirmation = input(f'Type DELETE {len(to_delete)} to confirm: ').strip()
        if confirmation != f'DELETE {len(to_delete)}':
            print('Cancelled: nothing deleted.')
            return

        removed = 0
        for d in to_delete:
            # Do not remove a record whose verified wallet fields changed.
            result = col.delete_one({
                '_id': d['_id'], 'franchise_id': d['franchise_id'],
                'user_id': d['user_id'], 'balance': 0, 'withdrawable_balance': 0,
                'referred_by': None, 'referral_bonus_paid': False,
                'joined_at': d['joined_at'],
            })
            if result.deleted_count != 1:
                raise SystemExit(f'STOP: A record changed during cleanup at {d["_id"]}; {removed} previously removed. Review backup.')
            removed += 1

        col.create_index([('franchise_id', ASCENDING), ('user_id', ASCENDING)],
                         unique=True, name='unique_franchise_user_wallet')
        print(f'SUCCESS: {removed} duplicates removed, 2 original wallets retained; unique index enforced.')
        print('Next run: python3 audit_referral_safety.py')
    finally:
        client.close()


if __name__ == '__main__':
    main()
