import sys
import tempfile
from pathlib import Path
import pytest

# ensure root in sys.path for both `pytest` and `python -m pytest`
sys.path.insert(0, str(Path(__file__).parent.parent))
import core.profile_manager as pm

@pytest.fixture(autouse=True)
def isolate_profiles(tmp_path):
    """Изолирует PROFILES_ROOT на каждый тест — защита от глобальной гонки между файлами."""
    orig = pm.PROFILES_ROOT
    isolated = tmp_path / "profiles"
    isolated.mkdir(parents=True, exist_ok=True)
    pm.PROFILES_ROOT = isolated
    try:
        yield isolated
    finally:
        pm.PROFILES_ROOT = orig
        # tmp_path удалится автоматически
