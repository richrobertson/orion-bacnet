## What and why

<!-- What does this change, and why? Link the issue if there is one. -->

## How it was tested

- [ ] `python -m pytest tests -q -p no:cacheprovider`
- [ ] `ruff check .`
- [ ] `shellcheck install.sh systemd/*.sh` (if shell changed)
- [ ] On a Raspberry Pi (`sudo ./install.sh`, `python -m water_bacnet --check`), if it touches install, systemd, rtl_433 or BACnet

## Checklist

- [ ] No new runtime dependencies (stdlib + pinned bacpypes3 only)
- [ ] Still Python 3.11 compatible
- [ ] README / config.example.toml updated if behaviour or config keys changed
- [ ] Units stay cubic feet (see docs/DESIGN.md)
