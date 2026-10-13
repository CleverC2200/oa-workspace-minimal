"""Explicit supplier selection for recorded tests of the retained CLI transport."""
import pytest


@pytest.fixture(autouse=True)
def retained_deepseek_cli(monkeypatch):
    from apps.oa_finance_preaudit import codex_review_worker, production_config
    monkeypatch.setenv('OA_REVIEW_PROVIDER', 'deepseek')
    monkeypatch.setenv('OA_REVIEW_TRANSPORT', 'cli')
    profile = production_config.review_settings('deepseek', transport='cli')
    monkeypatch.setattr(codex_review_worker, 'REVIEW_SETTINGS', profile)
    monkeypatch.setattr(codex_review_worker, 'MODEL', profile['model'])
    monkeypatch.setattr(codex_review_worker, 'REASONING_EFFORT', profile['reasoning_effort'])
