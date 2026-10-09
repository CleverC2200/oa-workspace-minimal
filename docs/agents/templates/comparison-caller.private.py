"""Thin caller for the existing private twenty-pair comparison, no key loader."""
from pathlib import Path
import hashlib
import json
import os
import sys

sys.dont_write_bytecode = True
os.umask(0o077)
repository, configuration_file, mode, request_file = sys.argv[1:5]
repository = Path(repository).absolute()
configuration_file = Path(configuration_file).absolute()
if repository != repository.resolve() or configuration_file != configuration_file.resolve():
    raise ValueError('comparison_configuration_path_invalid')
sys.path.insert(0, str(repository))
from apps.oa_finance_preaudit import review_batch_report as batch, review_experiment as e  # noqa: E402
from apps.oa_finance_preaudit.flow_evals import _read_bound_artifact  # noqa: E402

configuration = batch._object(configuration_file.parent, configuration_file)
if configuration['repository'] != str(repository):
    raise ValueError('comparison_repository_not_bound')
module = repository / 'apps/oa_finance_preaudit/review_comparison.py'
if configuration['module'] != {'file': str(module), 'sha256': batch._sha(module)}:
    raise ValueError('comparison_module_not_bound')
_read_bound_artifact(configuration['module'], document=False)
reference_module = repository / 'apps/oa_finance_preaudit/review_references.py'
if configuration['reference_module'] != {'file': str(reference_module), 'sha256': batch._sha(reference_module)}:
    raise ValueError('reference_module_not_bound')
_read_bound_artifact(configuration['reference_module'], document=False)
from apps.oa_finance_preaudit.review_comparison import _ComparisonCaller  # noqa: E402

caller = _ComparisonCaller(caller_file=Path(__file__),
                           identity_bank=configuration['identity_bank'],
                           adapter_file=configuration['adapter'],
                           reference_module=configuration['reference_module'])
request_path = Path(request_file).absolute()
request = batch._object(request_path.parent, request_path)
if mode in {'validate', 'run'}:
    authority = _read_bound_artifact(request['authority'])
    if authority.get('caller_configuration_sha256') != hashlib.sha256(configuration_file.read_bytes()).hexdigest():
        raise ValueError('comparison_configuration_not_authorized')
try:
    if mode == 'readiness':
        result = caller.inspect_readiness(request)
    elif mode in {'validate', 'run'}:
        result = caller.validate_or_run(request, run=mode == 'run')
    else:
        raise ValueError('private_comparison_mode_invalid')
except (OSError, ValueError, KeyError, TypeError) as error:
    # Native transport returns its owned count/unknown receipt in-band. These
    # admission exceptions occur before this invocation crosses that transport.
    claimed = False
    if request.get('scope'):
        claimed = (Path(request['scope']['file']).parent / 'candidate-phase-claim.private.json').exists()
    result = {'status': 'rejected', 'reason': str(error), 'phase_claim_written': claimed,
              'new_cli_starts': 0,
              'financial_qualification': False, 'new_oa_writes': 0}
print(json.dumps(result, ensure_ascii=False, allow_nan=False))
