# encoding: utf-8
"""`python -m sketchup_bridge` 的進入點（CLI）。"""

import sys

from .cli import main

if __name__ == "__main__":
    sys.exit(main())
