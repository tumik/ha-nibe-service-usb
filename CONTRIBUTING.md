# Contributing

Thanks for helping. This repository is the Home Assistant integration only; the gateway server
lives in [nibe-service-usb](https://github.com/tumik/nibe-service-usb) (send gateway problems
and new gateway features there).

## Setup

```
python3 -m venv .venv
.venv/bin/pip install pytest-homeassistant-custom-component ruff
```

Python 3.13 is required (as for current Home Assistant).

## Tests and style

```
.venv/bin/python -m pytest -q tests
.venv/bin/ruff check custom_components tests
```

- Tests use a fake gateway (`tests/fake_server.py`) on 127.0.0.1; no hardware or network needed.
  For manual testing against something that looks like a pump, run `nibe-service-usb simulate`
  from the gateway repository.
- Ruff settings are in `pyproject.toml` (line length 110). Keep type hints and the existing module
  structure (one platform per file, connection logic in `gateway.py`, wire protocol in
  `client.py`).
- User-visible strings go in `strings.json`; keep `translations/en.json` identical to it.

## Pull requests

- Keep a pull request focused on one change and describe what and why.
- Add or update tests for behaviour changes; tests and ruff must pass (CI also runs hassfest and
  the HACS validation).
- Update `README.md` and `CHANGELOG.md` (under "Unreleased") when behaviour or options change.
- Only use Home Assistant APIs available in the minimum version in `hacs.json`; if you need a
  newer one, say so in the pull request and raise the minimum deliberately.
- Do not commit keys, real server ids, addresses of your network or diagnostics files.

## Releases

Releases are automated by the `release` job in `.github/workflows/validate.yml`:

- Every push to `main` (or a manual **Run workflow** on `main`) whose hassfest, HACS and test jobs pass is released.
- If the version in `custom_components/nibe_service_usb/manifest.json` has not been tagged yet,
  it is released as is (tag `vX.Y.Z` and a GitHub release with generated notes).
- If it has already been released, the patch number is bumped in `manifest.json` and
  `pyproject.toml`, committed as "Release X.Y.Z" by the GitHub Actions bot, tagged and released.
- For a minor or major release, set the new version (for example `0.3.0`) in both files in your
  change; the job then releases that version without bumping it.
