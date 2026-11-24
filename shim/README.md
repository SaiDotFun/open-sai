# Sai Native Shim

The `sai-cli` wheel in this directory gives `uv tool` and `uvx` a Python
entry point for the native Sai CLI. The shim uses its own package version to
select the `sai-cli/vVERSION` tag and the archive for the current platform.
It checks `SHA256SUMS` before extracting the binary. The verified executable
lives at `$SAI_HOME/bin/shim/VERSION/TARGET/ARCHIVE_SHA256/sai` and runs
without network access on subsequent launches.

The Native Distribution workflow builds the shim, the four native archives,
and `SHA256SUMS` as the `sai-native-ci` Actions artifact. The Linux runners
build and smoke on glibc 2.35. The macOS builds target macOS 11 and run the
smoke tests on macOS 15.
