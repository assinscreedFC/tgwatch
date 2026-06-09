"""Fixtures pytest partagées pour l'ensemble de la suite tgwatch.

Ces fixtures sont disponibles dans tous les fichiers tests/ sans import explicite.
Les fixtures locales d'un test_*.py ont priorité sur ces fixtures de même nom.
"""

import pytest

from tgwatch.core.storage import Storage
from tgwatch.core.recorder import Recorder


@pytest.fixture()
def tmp_db(tmp_path):
    """Chemin vers une base SQLite temporaire (str)."""
    return str(tmp_path / "tgwatch.db")


@pytest.fixture()
def storage(tmp_db):
    """Storage initialisé sur DB temporaire — I/O réelles (règle projet)."""
    return Storage(tmp_db)


@pytest.fixture()
def recorder(storage):
    """Recorder injecté avec le storage temporaire."""
    return Recorder(storage)
