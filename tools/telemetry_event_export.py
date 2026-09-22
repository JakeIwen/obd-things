#!/usr/bin/env python3
"""Export a consistent, read-only backup of telemetry event evidence to tmp/.

No CAN, service access, or raw historian sample scan. For a large live historian,
run through a reviewed compute task; browser exports handle individual events.
The JSONL archive includes a manifest, episodes, transitions, compact checkpoints,
windows, notifications, annotations, and all referenced baseline bucket inputs.
"""
import argparse
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import sqlite3
import sys
import zlib

REPO=Path(__file__).resolve().parents[1]
if str(REPO) not in sys.path:sys.path.insert(0,str(REPO))
from projects.vehicle_data.event_history import GUIDE, VERSION

TABLES=('advisory_episodes','advisory_episode_events','advisory_evidence',
        'advisory_notification_outbox','advisory_annotations','advisory_baselines')

def export(database, destination):
    destination=Path(destination)
    destination.parent.mkdir(parents=True,exist_ok=True)
    # Exclusive create prevents an accidental overwrite of an earlier backup.
    conn=sqlite3.connect(Path(database).resolve().as_uri()+'?mode=ro',uri=True)
    conn.row_factory=sqlite3.Row
    conn.execute('PRAGMA query_only=ON')
    conn.execute('BEGIN')
    digest=hashlib.sha256();counts={}
    try:
        with destination.open('x',encoding='utf-8') as output:
            def write(record):
                raw=json.dumps(record,sort_keys=True,separators=(',',':'),ensure_ascii=False)+'\n'
                output.write(raw);digest.update(raw.encode())
            write({'kind':'manifest','schema_version':VERSION,'created_at':datetime.now(timezone.utc).isoformat(),
                   'system_guide':GUIDE,'consistency':'single SQLite read transaction',
                   'scope':'event evidence; does not include the full raw historian or claim to be an off-device backup'})
            existing={r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
            for table in TABLES:
                if table not in existing:
                    write({'kind':'missing_table','table':table,'reason':'legacy_schema'})
                    counts[table]=None;continue
                count=0
                for row in conn.execute(f'SELECT * FROM {table}'):
                    data=dict(row)
                    if table=='advisory_baselines':data['inputs']=json.loads(zlib.decompress(data.pop('inputs_zlib')))
                    write({'kind':table,'record':data});count+=1
                counts[table]=count
            write({'kind':'complete','counts':counts,'preceding_sha256':digest.hexdigest()})
    finally:
        conn.close()
    return {'path':str(destination),'counts':counts,'sha256':digest.hexdigest()}


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--database',type=Path,default=Path('/var/lib/van-telemetry/history.sqlite3'))
    parser.add_argument('--out',type=Path,default=Path('tmp/vehicle_data/event-backups')/f"events-{datetime.now(timezone.utc):%Y%m%dT%H%M%SZ}.jsonl")
    args=parser.parse_args()
    print(json.dumps(export(args.database,args.out),indent=2))
if __name__=='__main__':main()
