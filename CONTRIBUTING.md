# Contributing

Thanks for helping. This is a small single-purpose appliance, so please open an issue
to discuss anything bigger than a bug fix before writing code.

- **Python 3.11+, standard library + `bacpypes3==0.0.110` only.** No new runtime
  dependencies; the Pi installs offline-friendly from a pinned venv.
- Set up and run the checks CI runs:

  ```sh
  python3 -m venv .venv
  .venv/bin/pip install --require-hashes -r requirements-dev.txt
  .venv/bin/python -m pytest tests -q -p no:cacheprovider
  .venv/bin/ruff check .
  shellcheck install.sh systemd/*.sh
  ```

- Coverage (CI fails below 80 %):
  `python -m coverage run -m pytest tests -q -p no:cacheprovider && python -m coverage report`

- Add or update tests for behaviour changes. Keep the code Python 3.11 compatible
  (a test enforces the grammar).
- The meter register is **cubic feet**, not gallons, and a flat register is normal.
  See `docs/DESIGN.md` before changing anything about units or staleness.
- Keep commits focused, and describe how you tested on real hardware if you did.
- Security problems: see [SECURITY.md](SECURITY.md), not the issue tracker.

By contributing you agree that your contributions are licensed under the MIT License.
