from pathlib import Path
import sys
import time

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from src.forward_operations import AlreadyRunningError, InterProcessLock

lock_path = Path(sys.argv[1])
mode = sys.argv[2]
try:
    with InterProcessLock(lock_path, timeout_seconds=0.3, command_name=f"worker-{mode}"):
        print("LOCKED", flush=True)
        if mode == "hold":
            time.sleep(30)
        elif mode == "short":
            time.sleep(0.5)
except AlreadyRunningError:
    print("CONTENDED", flush=True)
    raise SystemExit(23)
