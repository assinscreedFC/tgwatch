# Releasing

1. Bump `version` in `pyproject.toml`.
2. Build + publish:

   ```bash
   rm -rf dist
   python -m build
   python -m twine check dist/*
   python -m twine upload dist/*
   ```

   Auth: username `__token__`, password = a PyPI token scoped to `tgwatch`
   (or set `TWINE_USERNAME` / `TWINE_PASSWORD`).

3. Tag + GitHub release:

   ```bash
   git tag vX.Y.Z && git push origin vX.Y.Z
   gh release create vX.Y.Z --title "vX.Y.Z" --notes "..."
   ```

> The packaging leak guard (`tests/test_packaging.py` + CI `packaging-guard`)
> runs on every push and blocks internal files from entering the artifacts.
>
> To switch to token-free publishing later, register a Trusted Publisher at
> <https://pypi.org/manage/project/tgwatch/settings/publishing/>
> (workflow `release.yml`, environment `pypi`) and restore a release workflow.
