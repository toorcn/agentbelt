import sys, time
print("starting api...", flush=True); time.sleep(0.8)
print("Error: Missing required env var DATABASE_URL", file=sys.stderr, flush=True)
sys.exit(1)
