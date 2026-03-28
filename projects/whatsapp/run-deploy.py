#!/usr/bin/env python3
"""Wrapper to run deploy-code.sh from LaunchAgent, bypassing macOS bash restrictions."""
import subprocess, os
script = os.path.join(os.path.dirname(os.path.abspath(__file__)), "deploy-code.sh")
subprocess.run(["/bin/bash", script], check=False)
