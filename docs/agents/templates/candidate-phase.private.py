"""Thin private task caller. It grants no execution authority."""
import json,sys
from pathlib import Path
sys.path.insert(0,sys.argv[1])
from apps.oa_finance_preaudit import review_experiment
mode,scope,authority=sys.argv[2:5]
if mode == "run":
 result=review_experiment._run_candidate_phase(Path(scope),Path(authority),sys.argv[5],caller_file=Path(__file__))
elif mode == "status":
 result=review_experiment._candidate_phase_status(Path(scope),Path(authority))
elif mode == "finish":
 result=review_experiment._finish_candidate_phase(Path(scope),Path(authority))
else:
 raise ValueError("private_phase_mode_invalid")
print(json.dumps(result,ensure_ascii=False))
