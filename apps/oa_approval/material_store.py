from __future__ import annotations

from contextlib import ExitStack, contextmanager
from contextvars import ContextVar
from datetime import datetime, timezone
import fcntl
import hashlib
from io import BytesIO
import json
import os
from pathlib import Path
import re
import stat
from uuid import uuid4

from apps.oa_approval import material_inventory
from apps.oa_approval.material_inventory import discover as discover_project


DEFAULT_LIMIT_BYTES = 3_000_000_000
_ACTIVE_OPERATIONS = ContextVar('oa_material_operations', default=frozenset())
_OPERATION_BASELINES = ContextVar('oa_material_baselines', default={})
_AVAILABILITY_READS = ContextVar('oa_material_availability_reads', default={})


def default_root() -> Path:
    return Path(os.environ.get('OA_PROJECT_SPACE_ROOT') or
                Path.home() / 'Library/Application Support/oa-project-space').expanduser() / 'materials'


def _path(value: Path) -> Path:
    path = material_inventory.canonical_system_path(Path(value).expanduser())
    if '..' in path.parts or any(parent.is_symlink() for parent in (path, *path.parents)):
        raise ValueError('material_path_untrusted')
    return path


def _digest(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open('rb') as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b''):
            digest.update(block)
    return digest.hexdigest()


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _source(value: dict | None) -> dict:
    result = {}
    for key in ('request_id', 'resource_id', 'flow_key', 'kind', 'parent_resource_id'):
        text = str((value or {}).get(key) or '')
        if text and re.fullmatch(r'[A-Za-z0-9_.:/-]{1,256}', text):
            result[key] = text
    return result


class MaterialStore:
    def __init__(self, root: Path | None = None, *, limit_bytes: int | None = None,
                 checkout_root: Path | None = None):
        self.root = _path(root if root is not None else default_root())
        self.checkout_root = Path(checkout_root) if checkout_root is not None else material_inventory.default_checkout_root()
        self._writable_experiments: set[Path] = set()
        self.limit_bytes = DEFAULT_LIMIT_BYTES if limit_bytes is None else limit_bytes
        if type(self.limit_bytes) is not int or not 1 <= self.limit_bytes <= 3_000_000_000:
            raise ValueError('material_budget_invalid')

    @contextmanager
    def _locked(self, *, readonly: bool = False, refresh: bool = True):
        scope = _AVAILABILITY_READS.get().get(str(self.root))
        if scope is not None:
            if not readonly:
                raise RuntimeError('material_read_write_forbidden')
            _path(self.root)
            if scope['state'] is None and self.root.exists():
                scope['state'] = scope['leases'].enter_context(self._inventory_locked(readonly=True, refresh=False))
            if scope['state'] is not None:
                yield scope['state']
                return
        with self._inventory_locked(readonly=readonly, refresh=refresh) as state:
            yield state

    @contextmanager
    def _inventory_locked(self, *, readonly: bool, refresh: bool):
        if readonly and not self.root.exists():
            yield {"schema_version": 1, "files": {}, "events": [], "directories": {}}
            return
        if not readonly:
            self.root.mkdir(parents=True, mode=0o700, exist_ok=True)
            self.root.chmod(0o700)
        _path(self.root)
        descriptor = os.open(self.root / 'inventory.lock', os.O_RDWR | os.O_NOFOLLOW | (0 if readonly else os.O_CREAT), 0o600)
        with os.fdopen(descriptor, 'r+') as lock:
            fcntl.flock(lock, fcntl.LOCK_EX)
            try:
                path = _path(self.root / 'inventory.json')
                state = json.loads(path.read_text()) if path.exists() else {'schema_version': 1, 'files': {}, 'events': [], 'directories': {}}
                if (state.get('schema_version') != 1 or not isinstance(state.get('files'), dict)
                        or not isinstance(state.get('events'), list)):
                    raise ValueError('material_inventory_invalid')
                state.setdefault('directories', {})
                state.setdefault('project_scopes', {})
                state.setdefault('project_checkouts', [])
                self._seal_registered_experiments(state)
                if refresh and str(self.root) not in _ACTIVE_OPERATIONS.get():
                    self._refresh(state)
                self._recover(state)
                yield state
            finally:
                fcntl.flock(lock, fcntl.LOCK_UN)

    @contextmanager
    def availability_read(self):
        if str(self.root) in _AVAILABILITY_READS.get():
            yield
            return
        with ExitStack() as leases:
            scope = {'state': None, 'leases': leases}
            token = _AVAILABILITY_READS.set({**_AVAILABILITY_READS.get(), str(self.root): scope})
            try:
                yield
            finally:
                _AVAILABILITY_READS.reset(token)

    @contextmanager
    def operation(self):
        active = _ACTIVE_OPERATIONS.get()
        key = str(self.root)
        if key in active:
            yield
            return
        with self._locked(refresh=False) as state:
            snapshot = discover_project(self.checkout_root)
            self._enroll_project(state, snapshot)
            self._refresh(state, skip_scopes=set(snapshot['scopes']))
            for entry in state['files'].values():
                entry['bytes'] = self._size(entry)
            self._save(state)
            baseline = frozenset(state['files'])
        token = _ACTIVE_OPERATIONS.set(active | {key})
        baseline_token = _OPERATION_BASELINES.set({**_OPERATION_BASELINES.get(), key: baseline})
        try:
            yield
        finally:
            _OPERATION_BASELINES.reset(baseline_token)
            _ACTIVE_OPERATIONS.reset(token)

    def _enroll_project(self, state: dict, snapshot: dict) -> None:
        state['project_scopes'].update(snapshot['scopes'])
        state['project_checkouts'] = sorted(set(state['project_checkouts']) | {str(root) for root in snapshot['checkouts']})
        for path in snapshot['files']:
            self._register_file(state, path, source={'kind': 'legacy'})

    def adopt_project(self, root: Path) -> dict:
        snapshot = material_inventory.discover(root)
        with self._locked(refresh=False) as state:
            self._enroll_project(state, snapshot)
            self._refresh(state, skip_scopes=set(snapshot['scopes']))
            self._save(state)
            return {'registered_count': len(snapshot['files']), 'used_bytes': self._used(state)}

    def _save(self, state: dict) -> None:
        temporary = self.root / ('.inventory-' + uuid4().hex)
        descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
        with os.fdopen(descriptor, 'w') as stream:
            json.dump(state, stream, ensure_ascii=False)
            stream.flush()
            os.fsync(stream.fileno())
        temporary.replace(_path(self.root / 'inventory.json'))
        descriptor = os.open(self.root, os.O_RDONLY)
        try:
            os.fsync(descriptor)
        finally:
            os.close(descriptor)

    def _size(self, entry: dict) -> int:
        path = _path(Path(entry['path']))
        try:
            info = path.stat()
        except FileNotFoundError:
            return entry['bytes'] if entry['status'] == 'reserved' else 0
        if not stat.S_ISREG(info.st_mode):
            raise ValueError('material_path_untrusted')
        return max(info.st_size, entry['bytes']) if entry['status'] == 'reserved' else info.st_size

    def _used(self, state: dict) -> int:
        baseline = _OPERATION_BASELINES.get().get(str(self.root), frozenset())
        return sum(entry['bytes'] if key in baseline else self._size(entry)
                   for key, entry in state['files'].items())

    def _seal_experiment(self, directory: Path) -> None:
        if stat.S_IMODE(directory.stat().st_mode) != 0o500:
            directory.chmod(0o500)
        for current, directories, files in os.walk(directory, followlinks=False):
            for name in directories:
                path = _path(Path(current) / name)
                if stat.S_IMODE(path.stat().st_mode) != 0o500:
                    path.chmod(0o500)
            for name in files:
                path = _path(Path(current) / name)
                if stat.S_IMODE(path.stat().st_mode) != 0o400:
                    path.chmod(0o400)

    def _seal_registered_experiments(self, state: dict) -> None:
        experiments = self.root / 'experiments'
        for raw in state['directories']:
            directory = _path(Path(raw))
            if directory.is_relative_to(experiments) and len(directory.relative_to(experiments).parts) == 1:
                if directory.exists():
                    if not directory.is_dir():
                        raise ValueError('material_path_untrusted')
                    self._seal_experiment(directory)

    @contextmanager
    def _writable_output(self, state: dict, path: Path):
        experiments = self.root / 'experiments'
        directory = None
        outer = False
        if path.is_relative_to(experiments):
            parts = path.relative_to(experiments).parts
            if len(parts) < 2:
                raise ValueError('material_workspace_invalid')
            directory = _path(experiments / parts[0])
            if str(directory) not in state['directories']:
                raise ValueError('material_workspace_unregistered')
            outer = directory not in self._writable_experiments
            self._writable_experiments.add(directory)
            directory.chmod(0o700)
            for parent in reversed(path.parent.parents):
                if parent.is_relative_to(directory) and parent.exists():
                    _path(parent).chmod(0o700)
            if path.parent.exists():
                _path(path.parent).chmod(0o700)
        try:
            path.parent.mkdir(parents=True, mode=0o700, exist_ok=True)
            _path(path)
            yield
        finally:
            if directory is not None and outer:
                self._writable_experiments.remove(directory)
                self._seal_experiment(directory)

    def _recover(self, state: dict) -> None:
        for key, entry in list(state['files'].items()):
            path = _path(Path(entry['path']))
            if entry['status'] in {'reserved', 'writing'}:
                if entry.get('external'):
                    continue
                if path.exists():
                    entry.update(status='protected', bytes=path.stat().st_size, sha256='')
                else:
                    del state['files'][key]
            elif entry['status'] == 'deletion_planned' and not path.exists():
                entry['status'] = 'retired'
                for event in state['events']:
                    if event['path'] == str(path) and event['status'] == 'planned':
                        event.update(status='completed', recovered=True)

    @contextmanager
    def _eligible(self, entry: dict):
        descriptor = self._lease_descriptor(Path(entry['path']))
        with os.fdopen(descriptor, 'r+') as lock:
            try:
                fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                yield False
                return
            with self._owner_eligible(entry) as eligible:
                yield eligible

    def _lease_descriptor(self, path: Path) -> int:
        directory = _path(self.root / 'leases')
        directory.mkdir(mode=0o700, exist_ok=True)
        key = hashlib.sha256(str(path).encode()).hexdigest()
        return os.open(_path(directory / key), os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW, 0o600)

    @contextmanager
    def read_lease(self, paths):
        paths = sorted({_path(Path(path)) for path in paths}, key=str)
        scope = _AVAILABILITY_READS.get().get(str(self.root))
        if scope is not None and scope['state'] is None and not self.root.exists():
            scope['state'] = scope['leases'].enter_context(self._inventory_locked(readonly=False, refresh=False))
        with ExitStack() as stack:
            with self._locked(readonly=str(self.root) in _AVAILABILITY_READS.get()) as state:
                for path in paths:
                    entry = state['files'].get(str(path))
                    if entry and entry['status'] in {'retired', 'deletion_planned'}:
                        raise ValueError('material_retired')
                    lock = stack.enter_context(os.fdopen(self._lease_descriptor(path), 'r+'))
                    fcntl.flock(lock, fcntl.LOCK_SH)
            yield

    @contextmanager
    def _owner_eligible(self, entry: dict):
        path = _path(Path(entry['path']))
        if self._inside_source_checkout(path):
            yield False
            return
        if path.is_relative_to(self.root / 'experiments'):
            yield entry['status'] in {'disposable', 'deletion_planned'} and not self._protected(path)
            return
        owner = next((parent for parent in path.parents if (parent / 'state.json').exists()
                      or (parent / 'action.json').exists()), None)
        if owner is None:
            yield entry['status'] in {'disposable', 'deletion_planned'} and not self._protected(path)
            return
        if path.relative_to(owner).parts[0] not in {'attachments', 'prepared'}:
            yield False
            return
        if any(owner.rglob('*.cleanup.json')):
            yield False
            return
        with ExitStack() as stack:
            try:
                for lock_path in (owner.parents[3] / '.retention.lock', owner / 'prepare.lock', owner / 'complete.lock'):
                    descriptor = os.open(_path(lock_path), os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW, 0o600)
                    lock = stack.enter_context(os.fdopen(descriptor, 'r+'))
                    fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
                from apps.preaudit_state import read_state

                action = read_state(_path(owner / 'state.json'), strict_path_identity=True)
                allowed = (action['execution_state'] == 'closed'
                           and action['write_state'] in {'not_requested', 'submitted'}
                           and not any('recovery' in item.name or 'quarantine' in item.name
                                       for item in owner.iterdir()))
                if allowed:
                    entry['source'] = {**entry['source'], 'request_id': action['request_id'],
                                       'flow_key': action['workflow_key']}
            except (OSError, ValueError, KeyError):
                allowed = False
            yield allowed

    def _skip_reclamation(self, entry: dict, event: dict | None, reason: str) -> None:
        path = _path(Path(entry['path']))
        present = path.exists()
        entry['status'] = 'protected' if present else 'retired'
        entry['bytes'] = path.stat().st_size if present and path.is_file() else 0
        if event is not None:
            event.update(status='skipped', skip_reason=reason)

    def _apply_reclamation_batch(self, state: dict, batch: list[tuple[dict, dict]]) -> int:
        self._save(state)
        freed = 0
        try:
            for entry, event in batch:
                path = _path(Path(entry['path']))
                if not path.is_file():
                    self._skip_reclamation(entry, event, 'material_unavailable')
                    continue
                with self._eligible(entry) as eligible:
                    if not eligible:
                        self._skip_reclamation(entry, event, 'eligibility_changed')
                        continue
                    if _digest(path) != event['sha256']:
                        self._skip_reclamation(entry, event, 'material_changed')
                        continue
                    with self._writable_output(state, path):
                        path.unlink()
                    freed += event['bytes']
                    entry.update(status='retired', bytes=0)
                    event['status'] = 'completed'
        finally:
            self._save(state)
        return freed

    def _reclaim(self, state: dict, required_bytes: int) -> int:
        freed = 0
        used = self._used(state)
        candidates = iter(sorted(state['files'].values(), key=lambda item: (
            not Path(item['path']).is_relative_to(self.root / 'experiments'), item['created_at'])))
        pending = {event['path']: event for event in state['events'] if event['status'] == 'planned'}
        dirty = False
        while used + required_bytes > self.limit_bytes:
            batch = []
            planned_bytes = 0
            for entry in candidates:
                if entry['status'] in {'reserved', 'writing'}:
                    continue
                path = _path(Path(entry['path']))
                previous = pending.get(str(path))
                if not path.is_file():
                    self._skip_reclamation(entry, previous, 'material_unavailable')
                    dirty = True
                    continue
                with self._eligible(entry) as eligible:
                    if not eligible:
                        if entry['status'] == 'deletion_planned':
                            self._skip_reclamation(entry, previous, 'eligibility_changed')
                            dirty = True
                        continue
                    fingerprint = _digest(path)
                    expected = entry['sha256'] or (previous or {}).get('sha256')
                    if expected and fingerprint != expected:
                        self._skip_reclamation(entry, previous, 'material_changed')
                        dirty = True
                        continue
                    event = previous or {
                        'path': str(path), 'source': entry['source'], 'bytes': path.stat().st_size,
                        'sha256': fingerprint, 'time': _now(), 'reason': 'capacity', 'status': 'planned',
                    }
                    entry['status'] = 'deletion_planned'
                    if previous is None:
                        state['events'].append(event)
                    batch.append((entry, event))
                    planned_bytes += event['bytes']
                if len(batch) >= 256 or used + required_bytes - planned_bytes <= self.limit_bytes:
                    break
            if not batch:
                if dirty:
                    self._save(state)
                break
            freed += self._apply_reclamation_batch(state, batch)
            dirty = False
            used = self._used(state)
        return freed

    def _inside_source_checkout(self, path: Path) -> bool:
        return any((parent / '.git').exists() and path.relative_to(parent).parts[0] != 'runtime'
                   for parent in path.parents)

    def _protected(self, path: Path) -> bool:
        if self._inside_source_checkout(path):
            return True
        for parent in path.parents:
            state_file = parent / 'state.json'
            if state_file.exists() or (parent / 'action.json').exists():
                try:
                    state = json.loads(_path(state_file).read_text())
                except (OSError, ValueError):
                    return True
                return state.get('execution_state') != 'closed' or state.get('write_state') not in {'not_requested', 'submitted'}
        return False

    def report(self) -> dict:
        with self._locked(readonly=True) as state:
            used = self._used(state)
            return {'limit_bytes': self.limit_bytes, 'used_bytes': used,
                    'available_bytes': max(0, self.limit_bytes - used),
                    'over_budget': used > self.limit_bytes,
                    'protected_bytes': sum(self._size(entry) for entry in state['files'].values()
                                           if entry['status'] in {'protected', 'writing', 'reserved'}),
                    'retired_count': sum(entry['status'] == 'retired' for entry in state['files'].values())}

    def _refresh(self, state: dict, *, skip_scopes: set[str] | None = None) -> None:
        allowed_roots = [Path(path) for path in state.get('project_checkouts', [])]
        for raw, all_material in state.get('project_scopes', {}).items():
            if raw in (skip_scopes or set()):
                continue
            for path in material_inventory.scan_files(_path(Path(raw)), all_material=all_material,
                                                       allowed_roots=allowed_roots):
                self._register_file(state, path, source={'kind': 'legacy'})
        for raw, source in state['directories'].items():
            root = _path(Path(raw))
            for path in material_inventory.scan_files(root, all_material=True, allowed_roots=allowed_roots or [root]):
                self._register_file(state, path, source=source)

    def _register_file(self, state: dict, path: Path, *, source: dict) -> None:
        path = _path(path)
        if str(path) not in state['files']:
            if not stat.S_ISREG(path.stat().st_mode):
                raise ValueError('material_path_untrusted')
            state['files'][str(path)] = {
                'path': str(path), 'source': source, 'bytes': path.stat().st_size,
                'sha256': '', 'created_at': _now(), 'status': 'protected',
            }

    def registered_files(self) -> list[dict]:
        with self._locked(readonly=True) as state:
            return [dict(state['files'][key]) for key in sorted(state['files'])]

    def adopt_files(self, paths, *, source: dict | None = None, protected: bool = True) -> dict:
        if protected is not True:
            raise ValueError('material_evidence_protected')
        paths = [_path(Path(path)) for path in paths]
        with self._locked() as state:
            for path in paths:
                if not path.is_file() or not stat.S_ISREG(path.stat().st_mode):
                    raise ValueError('material_path_untrusted')
                previous = state['files'].get(str(path), {})
                state['files'][str(path)] = {
                    'path': str(path), 'source': _source(source) or previous.get('source', {}),
                    'bytes': path.stat().st_size, 'sha256': previous.get('sha256', ''),
                    'created_at': previous.get('created_at', _now()),
                    'status': previous['status'] if previous.get('status') in {'retired', 'deletion_planned'} else 'protected',
                }
            self._save(state)
            return {'registered_count': len(paths), 'used_bytes': self._used(state)}

    def registered_directories(self) -> list[str]:
        with self._locked(readonly=True) as state:
            return sorted(state['directories'])

    def require_available(self, path: Path) -> None:
        self.require_available_files([path])

    def require_available_files(self, paths) -> None:
        paths = [_path(Path(path)) for path in paths]
        with self._locked(readonly=True, refresh=False) as state:
            for path in paths:
                entry = state['files'].get(str(path))
                if entry and entry['status'] in {'retired', 'deletion_planned'}:
                    raise ValueError('material_retired')

    def retired_materials(self) -> list[dict]:
        with self._locked(readonly=True) as state:
            return [dict(event) for event in state['events']]

    def adopt_directory(self, path: Path, *, source: dict | None = None) -> dict:
        path = _path(path)
        if not path.is_dir() or path == self.root or self.root.is_relative_to(path):
            raise ValueError('material_path_untrusted')
        with self._locked() as state:
            state['directories'][str(path)] = _source(source)
            for item in material_inventory.scan_files(path, all_material=True):
                self._register_file(state, item, source=_source(source))
            self._save(state)
            return {'status': 'protected', 'used_bytes': self._used(state)}

    def adopt(self, path: Path, *, source: dict | None = None, disposable: bool = False,
              closed: bool = False, write_state: str = 'unknown') -> dict:
        path = _path(path)
        if not path.is_file() or not stat.S_ISREG(path.stat().st_mode):
            raise ValueError('material_path_untrusted')
        if disposable and (not closed or write_state not in {'confirmed', 'confirmed_no_write', 'not_applicable', 'not_requested', 'submitted'}
                           or self._protected(path)):
            raise ValueError('material_evidence_protected')
        with self._locked() as state:
            previous = state['files'].get(str(path), {})
            entry = {'path': str(path), 'source': _source(source) or previous.get('source', {}),
                     'bytes': path.stat().st_size, 'sha256': _digest(path),
                     'created_at': previous.get('created_at', _now()),
                     'status': 'disposable' if disposable else 'protected'}
            state['files'][str(path)] = entry
            self._save(state)
            return {'status': entry['status'], 'bytes': entry['bytes']}

    def mark_disposable(self, path: Path, *, closed: bool, write_state: str,
                        source: dict | None = None) -> dict:
        return self.adopt(path, source=source, disposable=True, closed=closed, write_state=write_state)

    def discard_duplicate(self, path: Path, *, original: Path) -> bool:
        path, original = _path(path), _path(original)
        baseline = _OPERATION_BASELINES.get().get(str(self.root))
        if baseline is None or str(path) in baseline or path == original:
            return False
        with self.read_lease([original]):
            with self._locked() as state:
                entry = state['files'].get(str(path))
                if not entry or entry['status'] != 'protected' or not path.is_file():
                    return False
                descriptor = self._lease_descriptor(path)
                with os.fdopen(descriptor, 'r+') as lock:
                    try:
                        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
                    except BlockingIOError:
                        return False
                    if _digest(path) != _digest(original):
                        return False
                    with self._writable_output(state, path):
                        path.unlink()
                    del state['files'][str(path)]
                    self._save(state)
                    return True

    def reclaim(self, *, required_bytes: int = 0) -> dict:
        if type(required_bytes) is not int or required_bytes < 0:
            raise ValueError('material_budget_invalid')
        with self._locked() as state:
            freed = self._reclaim(state, required_bytes)
            return {'freed_bytes': freed, 'used_bytes': self._used(state), 'limit_bytes': self.limit_bytes}

    def copy_stream(self, path: Path, stream, *, source: dict | None = None,
                    expected_bytes: int | None = None) -> Path:
        input_name = getattr(stream, 'name', None)
        inputs = [Path(input_name)] if isinstance(input_name, str) and Path(input_name).is_file() else []
        with self.operation():
            if not inputs:
                return self._copy_stream(path, stream, source=source, expected_bytes=expected_bytes)
            with self.read_lease(inputs):
                return self._copy_stream(path, stream, source=source, expected_bytes=expected_bytes)

    def _copy_stream(self, path: Path, stream, *, source: dict | None,
                     expected_bytes: int | None) -> Path:
        path = _path(path)
        _path(path)
        if expected_bytes is not None and (type(expected_bytes) is not int or expected_bytes < 0):
            raise ValueError('material_budget_invalid')
        with self._locked() as state:
            with self._writable_output(state, path):
                self._reclaim(state, expected_bytes or 0)
                if self._used(state) + (expected_bytes or 0) > self.limit_bytes:
                    raise ValueError('material_budget_exceeded')
                temporary = path.with_name('.' + path.name + '.' + uuid4().hex + '.part')
                entry = {'path': str(temporary), 'source': _source(source), 'bytes': 0, 'sha256': '',
                         'created_at': _now(), 'status': 'writing'}
                state['files'][str(temporary)] = entry
                if str(path) not in state['files'] or state['files'][str(path)]['status'] == 'retired':
                    state['files'][str(path)] = {**entry, 'path': str(path), 'status': 'protected'}
                self._save(state)
                remaining = self.limit_bytes - self._used(state)
                try:
                    descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
                    digest = hashlib.sha256()
                    written = 0
                    with os.fdopen(descriptor, 'wb', buffering=0) as output:
                        while block := stream.read(1024 * 1024):
                            if expected_bytes is not None and written + len(block) > expected_bytes:
                                raise ValueError('material_source_size_mismatch')
                            if len(block) > remaining:
                                self._reclaim(state, len(block))
                                remaining = self.limit_bytes - self._used(state)
                            if len(block) > remaining:
                                raise ValueError('material_budget_exceeded')
                            output.write(block)
                            remaining -= len(block)
                            digest.update(block)
                            written += len(block)
                        output.flush()
                        os.fsync(output.fileno())
                    _path(path)
                    temporary.replace(path)
                    del state['files'][str(temporary)]
                    state['files'][str(path)] = {**entry, 'path': str(path), 'bytes': written,
                                                'sha256': digest.hexdigest(), 'status': 'protected'}
                    self._save(state)
                    return path
                except BaseException:
                    temporary.unlink(missing_ok=True)
                    state['files'].pop(str(temporary), None)
                    self._save(state)
                    raise

    @contextmanager
    def reserved_file(self, path: Path, *, max_bytes: int, source: dict | None = None, external: bool = False):
        path = _path(path)
        if type(max_bytes) is not int or max_bytes < 1:
            raise ValueError('material_budget_invalid')
        if max_bytes > self.limit_bytes:
            raise ValueError('material_budget_exceeded')
        with self.operation():
            with self._locked() as state:
                with self._writable_output(state, path):
                    if path.exists():
                        raise ValueError('material_output_exists')
                    self._reclaim(state, max_bytes)
                    if self._used(state) + max_bytes > self.limit_bytes:
                        raise ValueError('material_budget_exceeded')
                    entry = {'path': str(path), 'source': _source(source), 'bytes': max_bytes,
                             'sha256': '', 'created_at': _now(), 'status': 'reserved', 'external': external}
                    state['files'][str(path)] = entry
                    self._save(state)
                    try:
                        yield path
                        _path(path)
                        if path.exists() and (not path.is_file() or path.stat().st_size > max_bytes):
                            raise ValueError('material_output_limit_exceeded')
                    finally:
                        if path.exists():
                            _path(path)
                            state['files'][str(path)] = {**entry, 'bytes': path.stat().st_size,
                                                        'sha256': _digest(path), 'status': 'protected'}
                        else:
                            state['files'].pop(str(path), None)
                        self._save(state)

    def write_bytes(self, path: Path, data: bytes, *, source: dict | None = None) -> Path:
        return self.copy_stream(path, BytesIO(data), source=source, expected_bytes=len(data))

    def managed_directory(self, name: str) -> Path:
        if str(self.root) in _AVAILABILITY_READS.get():
            raise RuntimeError('material_read_write_forbidden')
        if not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9_.-]{0,79}', name):
            raise ValueError('material_workspace_invalid')
        path = _path(self.root / 'experiments' / name)
        path.mkdir(parents=True, mode=0o700, exist_ok=False)
        self.adopt_directory(path, source={'kind': 'experiment'})
        self._seal_experiment(path)
        return path


def write_bytes(path: Path, data: bytes, *, source: dict | None = None) -> Path:
    return MaterialStore().write_bytes(path, data, source=source)


def copy_stream(path: Path, stream, *, source: dict | None = None,
                expected_bytes: int | None = None) -> Path:
    return MaterialStore().copy_stream(path, stream, source=source, expected_bytes=expected_bytes)


@contextmanager
def availability_read():
    with MaterialStore().availability_read():
        yield
