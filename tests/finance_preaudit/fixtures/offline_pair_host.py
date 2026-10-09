"""Local process fixture only. No model, OA client, network, or credentials."""
import json
import os
from pathlib import Path
import signal
import subprocess
import sys

plan_file, arm, action = sys.argv[1:]
assert action == 'prepare'
assert 'OA_REVIEW_API_KEY' not in os.environ
plan = json.loads(Path(plan_file).read_text(encoding='utf-8'))
root = Path(plan['arms'][arm])
if not (root / 'local-process-start.json').exists():
    prepared = plan['preparations'][arm]
    if plan['offline_modes'][arm] == 'prepare_drift':
        prepared['delivery'] = 'deliberately-drifted-offline-fixture'
    print(json.dumps(prepared))
    raise SystemExit(0)
mode = plan['offline_modes'][arm]
child = None
cancelled = False
def cancel(_signum, _frame):
    global cancelled
    cancelled = True
    if child is not None:
        child.terminate()
signal.signal(signal.SIGTERM, cancel)
# The original parent masks SIGTERM around Popen ownership. Match its host
# contract by unmasking after installing a forwarding handler.
signal.pthread_sigmask(signal.SIG_UNBLOCK, {signal.SIGTERM})
child = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(' + ('0.02' if mode == 'exit' else '90') + ')'], env={'PYTHONDONTWRITEBYTECODE': '1'})
if cancelled:
    child.terminate()
pid_file = root / 'child-pid.json'
pid_file.write_text(json.dumps({'pid': child.pid}), encoding='utf-8')
try:
    child.wait(timeout=0.15 if mode == 'timeout' else 90 if mode in {'cancel', 'host_timeout'} else 3)
    state = 'cancelled' if cancelled else 'exited'
except subprocess.TimeoutExpired:
    child.terminate()
    child.wait(timeout=3)
    state = 'timed_out' if mode == 'timeout' else 'exited'
finally:
    if child.poll() is None:
        child.kill()
        child.wait()
result = {'financial_qualification': False, 'new_model_starts': 0,
          'process_state': state, 'child_pid': child.pid, 'child_exit_code': child.returncode,
          'child_reaped': child.poll() is not None, 'fixture_only': True}
(root / 'local-process-result.json').write_text(json.dumps(result), encoding='utf-8')
print(json.dumps(result))
