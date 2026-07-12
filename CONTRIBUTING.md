# Contributing to PartyPad

Thanks for helping improve PartyPad. The project is early-stage, so focused
bug reports and small, testable changes are especially valuable.

## Before opening an issue

- Search existing issues first.
- Include the host distribution, network manager, Wi-Fi adapter/driver, phone,
  browser, emulator version, and exact command used when relevant.
- For motion problems, run with `--log` and describe the phone pose and movement
  represented by the sample. Remove anything sensitive before attaching logs.

## Development workflow

1. Fork and clone the repository.
2. Run `uv sync`.
3. Make a focused change.
4. Run the unit tests and compile checks documented in the README.
5. Explain behavior changes and manual hardware testing in the pull request.

Do not commit generated certificates, private keys, motion logs, virtual
environments, or machine-specific emulator configuration.

## Adding a system

System selection is defined in `systems.py`. Keep the system, controller mode,
and emulator backend separable: several systems may share a controller family,
while one system may eventually need multiple core-specific devices. Leave new
registry entries marked unsupported until the layout, canonical mapping,
backend behavior, tests, and README status are all present. Never fall back to a
different system's controller without telling the user.

## Networking changes

Changes to `ap_helper.py` require extra care because it runs as root. Keep every
mutation scoped, reversible, and covered by cleanup on exceptions, signals, and
parent-process death. Never disconnect or reconfigure the station interface to
make AP setup easier. Document any new command or privilege requirement.
