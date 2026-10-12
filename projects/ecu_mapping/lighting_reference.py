"""Bounded offline analysis of the 2026-10-12 owner lighting reference."""

import argparse
from collections import defaultdict
from datetime import datetime
import hashlib
import json
from pathlib import Path

from lib.candump_io import parse_candump_line, identifier_bits
from lib.reports import atomic_json


def stamp(event):
    return datetime.fromisoformat(event['recorded_at']).timestamp()


def reference_windows(events):
    windows = []
    for index, event in enumerate(events):
        if not event['event'].startswith('owner_confirmed_'):
            continue
        baseline = event['event'] == 'owner_confirmed_lights_off_baseline'
        green = event.get('green_icon', False if baseline else None)
        blue = event.get('blue_icon', False if baseline else None)
        if type(green) is not bool or type(blue) is not bool:
            raise ValueError('each reference needs explicit boolean icon states')
        requested = next((x for x in events[index + 1:]
                          if x['event'].endswith('_requested')), None)
        end = stamp(requested) - 1 if requested else stamp(event) + 6
        windows.append({
            'name': event['event'], 'start': stamp(event) + 1, 'end': end,
            'green': green, 'blue': blue,
        })
    if len(windows) != 9 or any(w['end'] <= w['start'] for w in windows):
        raise ValueError('expected nine positive-duration owner reference windows')
    return windows


def collect(captures, windows):
    stats = defaultdict(dict)
    sources = []
    for path in captures:
        digest = hashlib.sha256()
        frames = 0
        with path.open('rb') as source:
            for line in source:
                digest.update(line)
                fields = parse_candump_line(line)
                ts = float(fields.timestamp)
                frames += 1
                index = next((i for i, w in enumerate(windows)
                              if w['start'] <= ts <= w['end']), None)
                if index is None:
                    continue
                cid = int(fields.can_id_text, 16)
                bits = identifier_bits(fields.can_id_text, cid, strict_width=True)
                if bits is None or not 1 <= len(fields.payload) <= 8:
                    raise ValueError('invalid classical frame identity or DLC')
                key = (fields.interface.decode(), bits, cid, len(fields.payload))
                value = int.from_bytes(fields.payload, 'little')
                row = stats[key].setdefault(index, {
                    'count': 0, 'and': value, 'or': value,
                    'first': ts, 'last': ts, 'example': fields.payload.hex(),
                })
                row['count'] += 1
                row['and'] &= value
                row['or'] |= value
                row['last'] = ts
        sources.append({'path': str(path), 'sha256': digest.hexdigest(),
                        'frames': frames, 'bytes': path.stat().st_size})
    return stats, sources


def candidates(stats, windows):
    found = []
    for key, rows in sorted(stats.items()):
        if len(rows) != len(windows) or any(r['count'] < 3 for r in rows.values()):
            continue
        for bit in range(key[3] * 8):
            mask = 1 << bit
            if any((r['and'] ^ r['or']) & mask for r in rows.values()):
                continue
            observed = [bool(rows[i]['and'] & mask) for i in range(len(windows))]
            for name in ('green', 'blue'):
                expected = [w[name] for w in windows]
                inverted = observed == [not value for value in expected]
                if observed != expected and not inverted:
                    continue
                found.append({
                    'channel': key[0], 'id_bits': key[1], 'can_id': key[2],
                    'can_id_hex': f'{key[2]:03X}', 'dlc': key[3],
                    'byte': bit // 8, 'bit_lsb0': bit % 8,
                    'indicator': name, 'inverted': inverted,
                    'window_counts': [rows[i]['count'] for i in range(len(windows))],
                    'examples': [rows[i]['example'] for i in range(len(windows))],
                    'window_values': observed,
                })
    return found


def transitions(captures, found):
    selected = defaultdict(list)
    for index, row in enumerate(found):
        selected[(row['channel'], row['id_bits'], row['can_id'], row['dlc'])].append(index)
    previous = {}
    changes = defaultdict(list)
    timing = {}
    for path in captures:
        with path.open('rb') as source:
            for line in source:
                fields = parse_candump_line(line)
                cid = int(fields.can_id_text, 16)
                bits = identifier_bits(fields.can_id_text, cid, strict_width=True)
                key = (fields.interface.decode(), bits, cid, len(fields.payload))
                if key not in selected:
                    continue
                ts = float(fields.timestamp)
                for index in selected[key]:
                    row = found[index]
                    state = bool(fields.payload[row['byte']] & (1 << row['bit_lsb0']))
                    state ^= row['inverted']
                    if index not in previous or state != previous[index]:
                        changes[index].append({'at': ts, 'on': state,
                                               'payload': fields.payload.hex()})
                    previous[index] = state
                    counts = timing.setdefault(index, {'count': 0, 'first': ts,
                                                       'last': ts, 'max_gap': 0})
                    counts['max_gap'] = max(counts['max_gap'], ts - counts['last'])
                    counts['last'] = ts
                    counts['count'] += 1
    for index, row in enumerate(found):
        row['transitions'] = changes[index]
        row['timing'] = timing[index]


def main(argv=None):
    parser = argparse.ArgumentParser()
    parser.add_argument('--events', type=Path, required=True)
    parser.add_argument('--output', type=Path, required=True)
    parser.add_argument('captures', type=Path, nargs='+')
    args = parser.parse_args(argv)
    if not 1 <= len(args.captures) <= 16:
        raise ValueError('capture count out of bounds')
    events = [json.loads(line) for line in args.events.read_text().splitlines()]
    windows = reference_windows(events)
    stats, sources = collect(args.captures, windows)
    found = candidates(stats, windows)
    transitions(args.captures, found)
    result = {'windows': windows, 'sources': sources, 'candidates': found,
              'classification': 'owner-matching candidates; compare transitions before promotion',
              'event_sha256': hashlib.sha256(args.events.read_bytes()).hexdigest()}
    args.output.parent.mkdir(parents=True, exist_ok=True)
    atomic_json(args.output, result)
    print(json.dumps({'candidate_count': len(found), 'candidates': [
        {k: r[k] for k in ('channel', 'can_id_hex', 'dlc', 'byte', 'bit_lsb0',
                           'indicator', 'inverted')} for r in found]}, indent=2))


if __name__ == '__main__':
    main()
