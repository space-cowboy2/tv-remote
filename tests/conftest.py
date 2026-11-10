import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent))

from sim import Rig  # noqa: E402


@pytest.fixture
async def rig(tmp_path):
    """A hub with one simulated TV that is on, paired, with YouTube closed."""
    r = Rig(tmp_path)
    await r.add_tv()
    yield r
    await r.close()
