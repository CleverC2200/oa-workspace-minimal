import time

from apps.oa_approval import runtime_control
from apps.oa_finance_preaudit import approval_lifecycle, host_chat_review
from tests.finance_preaudit.test_host_chat_review import exchange


def test_existing_controller_observer_stops_expired_app_parent_without_review_waiter(exchange, monkeypatch):
    _, _, _, _, _, manifest, _ = exchange
    now = time.time()
    monkeypatch.setattr(time, 'time', lambda: now + host_chat_review.PARENT_HEARTBEAT_MAX_AGE + 1)
    with runtime_control.global_writer_lease():
        approval_lifecycle.heartbeat(manifest['run_id'], manifest['controller_instance_id'],
                                     set(), [], writer_active=True)
        assert runtime_control.stop_requested()
        assert runtime_control.writer_active(runtime_control.request_writer_lock_file(runtime_control.GLOBAL_WRITER_KEY))
        # This is the real controller observer with no review request or waiter.
        progress = approval_lifecycle._progress(manifest)
        assert progress['writer_active'] is True
