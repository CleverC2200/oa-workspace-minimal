from __future__ import annotations

import json
from pathlib import Path
import subprocess

from scripts import project_space


def checkout(tmp_path: Path) -> Path:
    root = tmp_path / 'oa-workspace-dev'
    root.mkdir()
    subprocess.run(['git', 'init', '-q', str(root)], check=True)
    return root


def test_status_reports_unregistered_material_without_creating_inventory(tmp_path, monkeypatch, capsys):
    root = checkout(tmp_path)
    pool = tmp_path / 'private'
    monkeypatch.setenv('OA_PROJECT_SPACE_ROOT', str(pool))
    attachment = root / 'runtime' / 'finance_preaudit_attachments' / 'private-name.pdf'
    attachment.parent.mkdir(parents=True)
    attachment.write_bytes(b'abcdefg')
    assert project_space.main(['--root', str(root), 'status']) == 1
    report = json.loads(capsys.readouterr().out)
    assert report['unregistered_bytes'] == 7
    assert report['observed_bytes'] == 7
    assert report['coverage'] == 'incomplete'
    assert 'private-name' not in json.dumps(report)
    assert str(root) not in json.dumps(report)
    assert not pool.exists()


def test_registration_counts_legacy_materials_but_excludes_nested_repositories_and_dependencies(tmp_path, monkeypatch, capsys):
    root = checkout(tmp_path)
    monkeypatch.setenv('OA_PROJECT_SPACE_ROOT', str(tmp_path / 'private'))
    for relative, data in {
        '.scratch/experiment/data.bin': b'1234',
        '.scratch/worktree/.git': b'gitdir: elsewhere',
        '.scratch/worktree/source.py': b'preserve source',
        '.scratch/.venv/site-packages/big.bin': b'dependency',
        'runtime/review/run/attempts/1/attachments/source.pdf': b'12345',
        'runtime/finance_preaudit_prepared/page.png': b'12',
        'runtime/image-compressions/old/output.png': b'123',
        'runtime/review/run/attempts/1/state.json': b'{}',
    }.items():
        path = root / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(data)
    assert project_space.main(['--root', str(root), 'register-checkout']) == 0
    registered = json.loads(capsys.readouterr().out)
    assert registered['registered_count'] == 4
    assert registered['used_bytes'] == 14
    assert project_space.main(['--root', str(root), 'status']) == 0
    report = json.loads(capsys.readouterr().out)
    assert report['observed_bytes'] == 14
    assert report['unregistered_bytes'] == 0
    assert report['protected_bytes'] == 14
    assert (root / '.scratch/worktree/source.py').exists()


def test_managed_import_and_explicit_finish_then_reclaim_use_shared_budget(tmp_path, monkeypatch, capsys):
    from apps.oa_approval.material_store import MaterialStore
    root = checkout(tmp_path)
    monkeypatch.setenv('OA_PROJECT_SPACE_ROOT', str(tmp_path / 'private'))
    assert project_space.main(['--root', str(root), 'experiment', '--name', 'trial']) == 0
    workspace = Path(json.loads(capsys.readouterr().out)['directory'])
    source = tmp_path / 'source.pdf'
    source.write_bytes(b'123456789')
    assert project_space.main(['--root', str(root), 'import', '--name', 'trial', '--file', str(source),
                               '--filename', 'copy.pdf']) == 0
    assert json.loads(capsys.readouterr().out)['bytes'] == 9
    assert project_space.main(['--root', str(root), 'finish', '--name', 'trial']) == 0
    assert json.loads(capsys.readouterr().out)['disposable_count'] == 1
    store = MaterialStore(limit_bytes=10)
    store.write_bytes(store.root / 'new.pdf', b'abcd')
    assert not (workspace / 'copy.pdf').exists()
    assert source.read_bytes() == b'123456789'
    assert store.report()['used_bytes'] == 4


def test_failed_import_does_not_copy_into_unregistered_or_escaping_workspace(tmp_path, monkeypatch, capsys):
    root = checkout(tmp_path)
    monkeypatch.setenv('OA_PROJECT_SPACE_ROOT', str(tmp_path / 'private'))
    source = tmp_path / 'source.pdf'
    source.write_bytes(b'123')
    assert project_space.main(['--root', str(root), 'import', '--name', 'unknown', '--file', str(source),
                               '--filename', 'copy.pdf']) == 1
    assert json.loads(capsys.readouterr().out)['reason'] == 'material_workspace_unregistered'
    assert project_space.main(['--root', str(root), 'experiment', '--name', 'trial']) == 0
    capsys.readouterr()
    assert project_space.main(['--root', str(root), 'import', '--name', 'trial', '--file', str(source),
                               '--filename', '../escape.pdf']) == 1
    assert json.loads(capsys.readouterr().out)['reason'] == 'material_filename_invalid'


def test_managed_import_refuses_unregistered_legacy_materials(tmp_path, monkeypatch, capsys):
    root = checkout(tmp_path)
    monkeypatch.setenv('OA_PROJECT_SPACE_ROOT', str(tmp_path / 'private'))
    assert project_space.main(['--root', str(root), 'experiment', '--name', 'trial']) == 0
    capsys.readouterr()
    legacy = root / 'runtime/raw/large.pdf'
    legacy.parent.mkdir(parents=True)
    legacy.write_bytes(b'legacy')
    source = tmp_path / 'source.pdf'
    source.write_bytes(b'new')
    assert project_space.main(['--root', str(root), 'import', '--name', 'trial', '--file', str(source),
                               '--filename', 'copy.pdf']) == 1
    assert json.loads(capsys.readouterr().out)['reason'] == 'project_materials_unregistered'


def test_status_includes_other_git_worktree_materials(tmp_path, monkeypatch, capsys):
    root = checkout(tmp_path)
    monkeypatch.setenv('OA_PROJECT_SPACE_ROOT', str(tmp_path / 'private'))
    subprocess.run(['git', '-C', str(root), '-c', 'user.email=test@example.invalid', '-c', 'user.name=Test',
                    'commit', '-q', '--allow-empty', '-m', 'fixture'], check=True)
    other = tmp_path / 'another-worktree'
    subprocess.run(['git', '-C', str(root), 'worktree', 'add', '-q', '--detach', str(other)], check=True)
    attachment = other / 'runtime' / 'old' / 'attachments' / 'source.pdf'
    attachment.parent.mkdir(parents=True)
    attachment.write_bytes(b'12345678')
    assert project_space.main(['--root', str(root), 'status']) == 1
    report = json.loads(capsys.readouterr().out)
    assert report['checkout_count'] == 2
    assert report['unregistered_bytes'] == 8
    assert not (tmp_path / 'private').exists()


def test_status_reports_capacity_even_when_all_registered_materials_are_protected(tmp_path, monkeypatch, capsys):
    root = checkout(tmp_path)
    monkeypatch.setenv('OA_PROJECT_SPACE_ROOT', str(tmp_path / 'private'))
    attachment = root / 'runtime' / 'attachments' / 'large.bin'
    attachment.parent.mkdir(parents=True)
    with attachment.open('wb') as stream:
        stream.truncate(3_000_000_001)
    assert project_space.main(['--root', str(root), 'register-checkout']) == 0
    capsys.readouterr()
    inventory = tmp_path / 'private/materials/inventory.json'
    before = inventory.read_bytes()
    assert project_space.main(['--root', str(root), 'status']) == 1
    report = json.loads(capsys.readouterr().out)
    assert report['observed_bytes'] == 3_000_000_001
    assert report['reasons'] == ['project_materials_over_budget']
    assert report['unregistered_count'] == 0
    assert inventory.read_bytes() == before
    assert attachment.stat().st_size == 3_000_000_001


def test_status_blocks_symlinked_material_directory_without_revealing_target(tmp_path, monkeypatch, capsys):
    root = checkout(tmp_path)
    monkeypatch.setenv('OA_PROJECT_SPACE_ROOT', str(tmp_path / 'private'))
    outside = tmp_path / 'business-secret'
    outside.mkdir()
    (root / 'runtime').mkdir()
    (root / 'runtime/attachments').symlink_to(outside, target_is_directory=True)
    assert project_space.main(['--root', str(root), 'status']) == 1
    output = capsys.readouterr().out
    assert json.loads(output)['reasons'] == ['project_space_path_untrusted']
    assert 'business-secret' not in output
    assert not (tmp_path / 'private').exists()


def test_quality_storage_rejects_new_unmanaged_scratch_writer(tmp_path, monkeypatch, capsys):
    from scripts import quality
    root = checkout(tmp_path)
    files = {
        'AGENTS.md': '[Space](docs/agents/project-space.md)',
        'docs/agents/project-space.md': 'Storage policy.',
        '.github/workflows/quality.yml': 'run: python -m scripts.quality storage',
        '.codex/environments/environment.toml': 'scripts.project_space status\nscripts.project_space experiment',
        'scripts/dev_environment.py': 'project_space.preflight(root)',
        'apps/oa_approval/attachments.py': 'material_store.MaterialStore()',
        'apps/oa_approval/attachment_extractors.py': 'material_store.MaterialStore()',
        'apps/oa_approval/image_compression.py': 'material_store.MaterialStore()',
        'apps/oa_approval/overseas_invoice.py': 'MaterialStore()',
        'apps/oa_finance_preaudit/flow_engine.py': 'material_store.MaterialStore()',
    }
    for relative, text in files.items():
        path = root / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text)
    monkeypatch.setattr(quality, 'PROJECT_ROOT', root)
    assert quality.main(['storage']) == 0
    capsys.readouterr()
    (root / 'scripts/unmanaged.py').write_text("Path('.scratch/new-data.bin').write_bytes(b'large')")
    assert quality.main(['storage']) == 1
    assert capsys.readouterr().out.strip() == 'unmanaged_scratch_path:scripts/unmanaged.py'
