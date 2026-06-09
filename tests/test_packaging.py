"""Garde-fou packaging : aucun fichier interne ne doit fuiter dans les artefacts pip.

Build réellement le sdist + wheel dans un dossier temporaire puis inspecte leur
contenu. Échoue si un motif interdit (docs de planning, config agents, secrets,
base de données, historique git) apparaît. Empêche toute régression de
configuration de build de republier des fichiers internes sur PyPI.
"""
from __future__ import annotations

import subprocess
import sys
import tarfile
import zipfile
from pathlib import Path

import pytest

# Motifs interdits dans tout artefact distribué. Match sur n'importe quel
# segment de chemin (insensible à la profondeur).
FORBIDDEN_SEGMENTS = (
    ".planning",
    ".claude",
    "CLAUDE.md",
    ".coverage",
    ".git",
    ".pytest_cache",
)
FORBIDDEN_SUFFIXES = (".db", ".db-wal", ".db-shm", ".pyc")

PROJECT_ROOT = Path(__file__).resolve().parent.parent


def _is_forbidden(member: str) -> bool:
    parts = Path(member).parts
    if any(seg in parts for seg in FORBIDDEN_SEGMENTS):
        return True
    return any(member.endswith(suffix) for suffix in FORBIDDEN_SUFFIXES)


@pytest.fixture(scope="module")
def built_dist(tmp_path_factory: pytest.TempPathFactory) -> Path:
    """Build sdist + wheel dans un répertoire isolé, retourne le dossier dist."""
    out = tmp_path_factory.mktemp("dist")
    result = subprocess.run(
        [sys.executable, "-m", "build", "--outdir", str(out)],
        cwd=PROJECT_ROOT,
        capture_output=True,
        text=True,
    )
    if result.returncode != 0:
        pytest.fail(f"build a échoué:\n{result.stdout}\n{result.stderr}")
    return out


def _sdist_members(dist: Path) -> list[str]:
    sdist = next(dist.glob("*.tar.gz"))
    with tarfile.open(sdist) as tar:
        return tar.getnames()


def _wheel_members(dist: Path) -> list[str]:
    wheel = next(dist.glob("*.whl"))
    with zipfile.ZipFile(wheel) as zf:
        return zf.namelist()


def test_sdist_contains_no_internal_files(built_dist: Path) -> None:
    leaked = [m for m in _sdist_members(built_dist) if _is_forbidden(m)]
    assert not leaked, f"Fichiers internes fuités dans le sdist : {leaked}"


def test_wheel_contains_no_internal_files(built_dist: Path) -> None:
    leaked = [m for m in _wheel_members(built_dist) if _is_forbidden(m)]
    assert not leaked, f"Fichiers internes fuités dans le wheel : {leaked}"


def test_sdist_ships_expected_top_level(built_dist: Path) -> None:
    """README + LICENSE doivent être présents (page PyPI + conformité licence)."""
    members = _sdist_members(built_dist)
    assert any(m.endswith("README.md") for m in members)
    assert any(m.endswith("LICENSE") for m in members)
