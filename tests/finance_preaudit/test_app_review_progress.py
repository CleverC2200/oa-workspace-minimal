from apps.oa_finance_preaudit import controller_handoff, host_chat_review as host
from scripts.finance_preaudit_run import read_execution
from tests.finance_preaudit.test_host_chat_review import exchange, start


def test_original_execution_progress_exposes_only_bound_current_app_requests(exchange):
    action, _, owner, _, _, manifest, _ = exchange
    thread, _, request = start(exchange)
    paths = list((controller_handoff.manifest_file().parent / 'executions').glob('*/execution.json'))
    assert len(paths) == 1
    receipt = paths[0]
    try:
        result = read_execution(receipt, owner_task_id=owner, invocation_id=receipt.parent.name,
                                tool_session_id='original-session')
        pending = result['review_requests']
        assert len(pending) == 1
        assert pending[0]['request_file'] == str(request)
        assert pending[0]['assignment']['slot'] == 0
        assert result['run_id'] == manifest['run_id']
    finally:
        host.close(owner=owner, reason='cancelled')
        thread.join(3)
        assert not thread.is_alive()
