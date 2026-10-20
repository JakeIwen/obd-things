"""candump_arrays keeps exact payloads per identifier and honours the epoch window."""

from pathlib import Path
import tempfile
import unittest

import numpy as np

from tools.candump_arrays import main


LOG = """(100.000000) can0 2ED#5A000000049C8000
(100.050000) can0 0FC#0B00000000000000
(100.100000) can0 2ED#5A0000000000
not a candump line
(200.000000) can0 18DAF110#0462023400000000
(300.000000) can0 2ED#5B00000000000000
"""


class CandumpArraysTests(unittest.TestCase):
    def test_payloads_lengths_and_window(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            log = root / 'drive.log'
            log.write_text(LOG)
            output = root / 'frames.npz'
            main([str(log), '--start', '100.0', '--end', '250.0', '--output', str(output)])
            arrays = np.load(output)
            self.assertEqual(arrays['t_2ED'].tolist(), [100.0, 100.1])
            self.assertEqual(arrays['d_2ED'][0].tolist(), [0x5A, 0, 0, 0, 0x04, 0x9C, 0x80, 0])
            self.assertEqual(arrays['d_2ED'][1].tolist(), [0x5A, 0, 0, 0, 0, 0, 0, 0])
            self.assertEqual(arrays['n_2ED'].tolist(), [8, 6])
            self.assertEqual(arrays['d_18DAF110'][0][:4].tolist(), [0x04, 0x62, 0x02, 0x34])
            self.assertEqual(sorted(k for k in arrays.files if k.startswith('t_')),
                             ['t_0FC', 't_18DAF110', 't_2ED'])


if __name__ == '__main__':
    unittest.main()
