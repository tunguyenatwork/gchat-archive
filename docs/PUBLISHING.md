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

## If `twine` is not installed

twine is a separate tool, not part of Python — `No module named twine` just means it is missing:

```bash
python3 -m pip install --user twine        # then: python3 -m twine upload dist/…
# or install it once and never think about it again:
pipx install twine && twine upload dist/gchat_archive-0.2.0*
```

## Trusted publishing (no token, no twine)

`.github/workflows/release.yml` already does this — build, `twine check`, install the wheel and run
`gchat-archive --help`, then publish with OIDC. It needs one one-time link on PyPI:

PyPI → *Your project → Publishing → Add a new publisher → GitHub*, with

| field | value |
|---|---|
| owner | `tunguyenatwork` |
| repository | `gchat-archive` |
| workflow name | `release.yml` |
| environment | `pypi` |

Then publishing is just:

```bash
git tag v0.2.0 && git push --tags
```

The workflow appears under the Actions tab and the run shows whether the publisher matched. Until
that publisher exists the upload step fails — it cannot publish without your authorisation.

## Checklist before the first upload

- [ ] `LICENSE` present and referenced in `pyproject.toml` (MIT — yes)
- [ ] `python -m build` produced both sdist and wheel
- [ ] wheel contains `gchat_archive/web/*` (the UI) — check with
      `python -c "import zipfile;print([n for n in zipfile.ZipFile('dist/gchat_archive-0.2.0-py3-none-any.whl').namelist() if '/web/' in n])"`
- [ ] `pip install dist/*.whl` in a clean venv, then `gchat-archive doctor`
- [ ] README no longer says "not on PyPI yet"
