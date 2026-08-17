#!/usr/bin/env python3
"""Dependency-free test runner (the host has no pytest). Runs every test_* in
every test_*.py module here. Usage: python3 tests/run.py"""

import importlib
import os
import sys
import traceback

HERE = os.path.dirname(__file__)
sys.path.insert(0, HERE)
sys.path.insert(0, os.path.join(HERE, ".."))

fails = total = 0
for fn in sorted(os.listdir(HERE)):
    if fn.startswith("test_") and fn.endswith(".py"):
        mod = importlib.import_module(fn[:-3])
        for name in sorted(dir(mod)):
            if name.startswith("test_"):
                total += 1
                try:
                    getattr(mod, name)()
                    print("ok  ", mod.__name__, name)
                except Exception:
                    fails += 1
                    print("FAIL", mod.__name__, name)
                    traceback.print_exc()

print(f"--- {total - fails}/{total} passed ---")
sys.exit(1 if fails else 0)
