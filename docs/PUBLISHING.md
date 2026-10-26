# Publishing to PyPI

The README documents `pipx install gchat-archive`, but that only works once a release exists on
PyPI. As of now the name `gchat-archive` is unclaimed (PyPI returns 404) and nothing has been
uploaded, so the documented one-liner fails with *"No matching distribution found"*.

Until the first upload, people install from git:

```bash
pipx install git+https://github.com/tunguyenatwork/gchat-archive.git
pipx inject gchat-archive pypdf python-docx openpyxl beautifulsoup4
```

---

## First release

You need a PyPI account and an API token. Never paste the token into a chat or commit it.

```bash
# 1. build the artifacts (done by the maintainer in a dev checkout)
python -m pip install build twine
python -m build                      # writes dist/*.whl and dist/*.tar.gz

# 2. check what will be uploaded
python -m twine check dist/*

# 3. upload — twine prompts for the token, username is __token__
python -m twine upload dist/gchat_archive-0.2.0*
```

Token comes from <https://pypi.org/manage/account/token/> (scope it to the project). Export it as
`TWINE_PASSWORD` rather than typing it into shell history if you prefer.

After that, `pipx install "gchat-archive[text]"` and `pip install gchat-archive` work for everyone,
and this file can be deleted from the README's "not on PyPI yet" note.

## Later releases

```bash
# bump the version in pyproject.toml and src/gchat_archive/__init__.py
python -m build
python -m twine upload dist/gchat_archive-0.3.0*
git tag v0.3.0 && git push --tags
```

## Trusted publishing (no long-lived token)

On PyPI → *Publishing* → add a GitHub publisher for `tunguyenatwork/gchat-archive` with workflow
`release.yml` and environment `pypi`. Then a tag push uploads without any secret:

```yaml
# .github/workflows/release.yml
name: release
on:
  push:
    tags: ["v*"]
jobs:
  build:
    runs-on: ubuntu-latest
    permissions: { id-token: write }
    steps:
      - uses: actions/checkout@v4
      - uses: actions/setup-python@v5
        with: { python-version: "3.12" }
      - run: pip install build && python -m build
      - uses: pypa/gh-action-pypi-publish@release/v1
```

## Checklist before the first upload

- [ ] `LICENSE` present and referenced in `pyproject.toml` (MIT — yes)
- [ ] `python -m build` produced both sdist and wheel
- [ ] wheel contains `gchat_archive/web/*` (the UI) — check with
      `python -c "import zipfile;print([n for n in zipfile.ZipFile('dist/gchat_archive-0.2.0-py3-none-any.whl').namelist() if '/web/' in n])"`
- [ ] `pip install dist/*.whl` in a clean venv, then `gchat-archive doctor`
- [ ] README no longer says "not on PyPI yet"
