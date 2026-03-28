#!/usr/bin/env python3
"""Thin Python wrapper to bypass macOS Full Disk Access restrictions on bash LaunchAgents."""
import subprocess
import sys
import os

script = os.path.join(os.path.dirname(os.path.abspath(__file__)), "health-check.sh")
result = subprocess.run(["/bin/bash", script], capture_output=False)
sys.exit(result.returncode)
