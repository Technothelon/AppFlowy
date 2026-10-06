# SPDX-License-Identifier: AGPL-3.0-only
"""Run upstream SVG and localization generators from the fork's frontend."""
import subprocess
from pathlib import Path

source = Path(__file__).resolve().parents[4] / 'frontend/appflowy_flutter'
for args in (
    ['run', 'flowy_svg'],
    ['run', 'easy_localization:generate', '-S', 'assets/translations/'],
    ['run', 'easy_localization:generate', '-f', 'keys', '-o', 'locale_keys.g.dart',
     '-S', 'assets/translations/', '-s', 'en-US.json'],
):
    subprocess.run(['dart', *args], cwd=source, check=True)
