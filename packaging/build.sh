#!/usr/bin/env bash
# Build standalone Windows .exe's (no Python install required to run them)
# for both the CLI and the GUI. Run from the repo root:
#   bash packaging/build.sh
#
# Output: dist/rpp-to-als.exe (CLI), dist/ReAbleton.exe (GUI)
#
# Why entry-point wrapper scripts (packaging/cli_entry.py, gui_entry.py)
# instead of pointing PyInstaller straight at rpp_to_als/cli.py or gui.py:
# those two use relative imports (`from .rpp_parse import ...`), which
# only work when the module is imported as part of the rpp_to_als
# package -- PyInstaller (like running the file directly with `python
# cli.py`) executes the target script as top-level __main__ with no
# parent package, which fails with "attempted relative import with no
# known parent package". The wrapper scripts live outside the package
# and do a normal `from rpp_to_als.cli import main`, so the package's
# own internals import correctly.
#
# --add-data bundles rpp_to_als/templates/*.xml (every .xml file
# als_builder.py/cli.py load via `os.path.join(os.path.dirname(__file__),
# "templates", ...)`) at the same relative path inside the frozen
# bundle, so that path resolution keeps working unchanged when frozen.
# Ableton's own real templates (track/device shells this tool clones)
# are NOT bundled -- they're read from the user's actual Ableton
# install at C:\ProgramData\Ableton\... at runtime, same as the
# non-frozen tool; this requires Ableton to be installed on the machine
# running the exe regardless of packaging.
set -euo pipefail
cd "$(dirname "$0")/.."
REPO_ROOT="$(pwd -W 2>/dev/null || pwd)"

rm -rf build dist

python -m PyInstaller --onefile --name rpp-to-als \
  --paths . \
  --specpath packaging \
  --add-data "$REPO_ROOT/rpp_to_als/templates;rpp_to_als/templates" \
  packaging/cli_entry.py

python -m PyInstaller --onefile --windowed --name ReAbleton \
  --paths . \
  --specpath packaging \
  --add-data "$REPO_ROOT/rpp_to_als/templates;rpp_to_als/templates" \
  packaging/gui_entry.py

echo
echo "Built: dist/rpp-to-als.exe (CLI), dist/ReAbleton.exe (GUI)"
