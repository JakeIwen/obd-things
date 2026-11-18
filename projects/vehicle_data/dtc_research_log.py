"""One JSON log line on stderr, shared by the advisor's research modules."""

import json
import sys


def log(event, **fields):
    print(json.dumps({"event": event, **fields}), file=sys.stderr, flush=True)
