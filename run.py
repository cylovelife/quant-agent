#!/usr/bin/env python3
"""Source-checkout compatibility launcher; use quant-agent after installation.

@author cylovelife
"""

import sys
from pathlib import Path

if __name__ == "__main__":
    sys.path.insert(0, str(Path(__file__).resolve().parent / "src"))
    from quant_agent.entrypoints.cli import main

    raise SystemExit(main())
