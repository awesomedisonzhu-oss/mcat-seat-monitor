"""Decides whether this wake-up should run a real check (random 15-25 minute gaps)."""
import json
import os
import time

try:
    next_run = json.load(open("state/state.json")).get("next_run", 0)
except Exception:
    next_run = 0

manual = os.environ.get("GITHUB_EVENT_NAME") != "schedule"
run = manual or time.time() >= next_run
print("Running a check now." if run else f"Next check in about {int((next_run - time.time()) / 60) + 1} min.")
with open(os.environ["GITHUB_OUTPUT"], "a") as f:
    f.write(f"run={'true' if run else 'false'}\n")
