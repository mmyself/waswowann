from __future__ import annotations

import argparse
from app.config import settings
from app.db import migrate
from app.services.demo_data import seed_demo
from app.services.local_import import import_local
from app.services.metacore_bridge import sync_legacy_observations


def main():
    parser=argparse.ArgumentParser(prog='wowannwas')
    parser.add_argument('command',choices=['migrate','seed-demo','import-local','sync-metacore'])
    args=parser.parse_args()
    migrate()
    if args.command=='migrate':
        print(f'Database migrated: {settings.database_path}')
    elif args.command=='seed-demo':
        print(f'{seed_demo()} Demo-Beobachtungen importiert.')
    elif args.command=='sync-metacore':
        stats=sync_legacy_observations(seed_plans=True)
        print(f"MetaCore synchronisiert ({stats['metacore_db_path']}): {stats['entities_synced']} Fund-Weltlinien, {stats['states_synced']} Zustände.")
    else:
        imported,skipped,failed=import_local()
        print(f'{imported} importiert, {skipped} bereits vorhanden, {failed} fehlgeschlagen.')

if __name__=='__main__': main()
