# Sai Native Shim

The `sai-cli` wheel in this directory gives `uv tool` and `uvx` a Python
entry point for the native Sai CLI. The shim uses its own package version to
select the `sai-cli/vVERSION` tag and the archive for the current platform.
It checks `SHA256SUMS` before extracting the binary. The verified executable
lives at `$SAI_HOME/bin/shim/VERSION/TARGET/ARCHIVE_SHA256/sai` and runs
without network access on subsequent launches.

The shim passes its Python `sys.prefix` as `SAI_NATIVE_UV_PREFIX` when
launching the native CLI. The native updater uses uv's receipt to tell
a persistent tool install from an isolated `uvx` run.

The Native Distribution workflow builds the shim, the four native archives,
and `SHA256SUMS` as the `sai-native-ci` Actions artifact. The Release workflow
uses that same build and smoke test matrix. Release tags ship the native
archives alongside the full Python packages. The shim uses the same wheel
and sdist filenames and stays in CI until the Python package handoff.
