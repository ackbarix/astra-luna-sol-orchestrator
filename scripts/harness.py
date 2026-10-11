#!/usr/bin/env python3
"""ALS local harness entrypoint; installed beside als_harness/."""
from pathlib import Path
import sys

if sys.version_info < (3, 9):
    raise SystemExit('ALS harness needs Python 3.9 or newer (found %d.%d). '
                     'Install a newer python3 and re-run this command with it.' % sys.version_info[:2])
here = Path(__file__).resolve().parent
if here.name == 'scripts':
    sys.path.insert(0, str(here.parent))
from als_harness.cli import main  # noqa: E402

if __name__ == '__main__':
    raise SystemExit(main())
