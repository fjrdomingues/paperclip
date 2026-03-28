#!/usr/bin/env python3
"""Thin Python wrapper to bypass macOS Full Disk Access restrictions on bash LaunchAgents."""
import os
import subprocess
import sys

script = os.path.join(os.path.dirname(os.path.abspath(__file__)), "daily-backup.sh")
result = subprocess.run(["/bin/bash", script], capture_output=False, check=False)
sys.exit(result.returncode)
