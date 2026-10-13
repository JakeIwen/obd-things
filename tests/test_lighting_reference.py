"""Owner-state evidence must match repeated windows without merging streams."""

from datetime import datetime, timezone
import json
from pathlib import Path
import tempfile
import unittest

from projects.ecu_mapping.lighting_reference import main


class LightingReferenceTests(unittest.TestCase):
    def test_output_cannot_replace_events_or_capture(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            events = root / 'events.jsonl'
            capture = root / 'capture.candump'
            events.write_text('owner evidence must be preserved\n')
            capture.write_text('(1790000000.0) can7 123#00\n')
            before = {path: path.read_bytes() for path in (events, capture)}
            for output in (events, capture):
                with self.subTest(output=output.name), self.assertRaisesRegex(
                        ValueError, 'output must differ'):
                    main(['--events', str(events), '--output', str(output), str(capture)])
                self.assertEqual({path: path.read_bytes() for path in before}, before)

    def test_public_report_matches_both_cycles_and_keeps_stream_identity(self):
        states = [(False, False), (True, False), (True, True), (True, False),
                  (False, False), (True, False), (True, True), (True, False),
                  (False, False)]
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            events = []
            frames = []
            for index, (green, blue) in enumerate(states):
                ts = 1_790_000_000 + index * 20
                events.append({
                    'recorded_at': datetime.fromtimestamp(ts, timezone.utc).isoformat(),
                    'event': f'owner_confirmed_{index}',
                    'green_icon': green, 'blue_icon': blue,
                })
                events.append({
                    'recorded_at': datetime.fromtimestamp(ts + 12, timezone.utc).isoformat(),
                    'event': f'next_{index}_requested',
                })
                for offset in (2, 3, 4):
                    value = int(green) | (int(blue) << 1) | (int(not blue) << 2)
                    value |= (offset % 2) << 3
                    frames.append(f'({ts + offset}.000000) can7 123#{value:02X}\n')
                    # The same numeric ID on another bus or namespace is unrelated.
                    frames.append(f'({ts + offset}.000000) can8 123#00\n')
                    frames.append(f'({ts + offset}.000000) can7 00000123#00\n')
                    # Insufficient coverage is not constant-state evidence.
                    if index != 6:
                        frames.append(f'({ts + offset}.000000) can7 124#{value:02X}\n')
            event_path = root / 'events.jsonl'
            event_path.write_text(''.join(json.dumps(e) + '\n' for e in events))
            capture = root / 'capture.candump'
            capture.write_text(''.join(frames))
            output = root / 'report.json'
            main(['--events', str(event_path), '--output', str(output), str(capture)])
            report = json.loads(output.read_text())
            found = report['candidates']
            self.assertEqual(len(found), 3)
            self.assertEqual({(r['channel'], r['id_bits'], r['can_id'], r['dlc'])
                              for r in found}, {('can7', 11, 0x123, 1)})
            self.assertEqual({(r['indicator'], r['bit_lsb0'], r['inverted'])
                              for r in found},
                             {('green', 0, False), ('blue', 1, False), ('blue', 2, True)})
            blue = next(r for r in found if r['bit_lsb0'] == 1)
            self.assertEqual([change['on'] for change in blue['transitions']],
                             [False, True, False, True, False])
            self.assertTrue(all(count == 3 for count in blue['window_counts']))

    def test_rejects_incomplete_reference_instead_of_guessing_states(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            events = root / 'events.jsonl'
            events.write_text('')
            capture = root / 'capture.candump'
            capture.write_text('(1790000000.0) can7 123#00\n')
            with self.assertRaisesRegex(ValueError, 'nine'):
                main(['--events', str(events), '--output', str(root / 'report.json'),
                      str(capture)])

    def test_missing_icon_reference_is_not_treated_as_off(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            events = root / 'events.jsonl'
            events.write_text(json.dumps({'event': 'owner_confirmed_unknown',
                                         'recorded_at': '2026-10-12T07:00:00+00:00'}) + '\n')
            capture = root / 'capture.candump'
            capture.write_text('')
            with self.assertRaisesRegex(ValueError, 'explicit boolean'):
                main(['--events', str(events), '--output', str(root / 'report.json'),
                      str(capture)])
