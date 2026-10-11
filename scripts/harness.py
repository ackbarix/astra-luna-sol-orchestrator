#!/usr/bin/env python3
"""ALS local harness entrypoint; installed beside als_harness/."""
from pathlib import Path
import sys

if sys.version_info < (3, 11):
    raise SystemExit('Python 3.11+ required')
here = Path(__file__).resolve().parent
if here.name == 'scripts':
    sys.path.insert(0, str(here.parent))
from als_harness.cli import main  # noqa: E402

if __name__ == '__main__':
    raise SystemExit(main())
