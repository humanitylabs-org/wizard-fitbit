"""The view is vanilla source: validation is the build, web/ is its dist."""
from pathlib import Path
root = Path(__file__).resolve().parent.parent
for name in ('index.html', 'boot.js', 'wizard-tokens.css', 'wizard-ui.css', 'wizard-sheet.js', 'standalone.css', 'styles.css', 'app.js'):
    assert (root / 'web' / name).is_file(), name
html = (root / 'web/index.html').read_text()
assert '/wizard-os-home/core/app-client.js' in html, 'Shared system client must load before app code'
assert html.index('/wizard-os-home/core/app-client.js') < html.index('app.js'), 'Shared client load order'
print('View built: web/ (vanilla source, no dependencies)')
