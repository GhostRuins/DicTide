# Contributing to DicTide

Thanks for helping improve DicTide.

## Development setup

```powershell
python -m venv .venv
.\.venv\Scripts\activate
python -m pip install -r requirements.txt -r requirements-build.txt
```

Run the app:

```powershell
python -m src.main
```

## Pull requests

- Keep PRs focused and small when possible.
- Include a clear description and test notes.
- Update README/changelog when behavior changes.
- Ensure the app launches and core record/transcribe flow works before submitting.
