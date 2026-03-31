#!/usr/bin/env python3
"""Entry point: run WhatsApp conversation flow handler."""
import subprocess
import sys
from pathlib import Path

SCRIPT_DIR = Path(__file__).parent
sys.exit(subprocess.call(
    [sys.executable, str(SCRIPT_DIR / "conversation_handler.py")] + sys.argv[1:],
    cwd=str(SCRIPT_DIR),
))
