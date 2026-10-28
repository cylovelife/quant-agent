"""Workspace files belong outside Python packages.

@author cylovelife
"""

import os
from pathlib import Path

PACKAGE_ROOT = Path(__file__).resolve().parents[1]
SOURCE_ROOT = PACKAGE_ROOT.parent.parent
WORKSPACE_ROOT = Path(os.environ.get("QUANT_AGENT_HOME", SOURCE_ROOT)).expanduser().resolve()
