from contextlib import contextmanager
from pathlib import Path
import shutil

import pytest


@pytest.fixture
def recorded_bounded_office_workspace(monkeypatch):
    from apps.oa_approval import bounded_staging

    @contextmanager
    def recorded_workspace(image_path: Path, *, max_bytes: int):
        image_path.write_bytes(b'recorded-volume')
        directory = image_path.parent / 'mounted'
        directory.mkdir()
        try:
            yield directory
        finally:
            shutil.rmtree(directory)

    monkeypatch.setattr(bounded_staging, 'workspace', recorded_workspace)
