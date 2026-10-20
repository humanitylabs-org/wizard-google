"""Packaging rules: bind-mount-only state, no secrets in source, a valid wizard.md, shared style untouched."""
import re
import subprocess
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
CHECKER = ROOT.parent.parent / 'conn-inv' / 'wizard-os' / 'tools' / 'check_wizard_md.py'


class Package(unittest.TestCase):
    def test_all_state_is_on_the_app_data_bind_mount(self):
        compose = (ROOT / 'runtipi' / 'docker-compose.yml').read_text()
        self.assertIsNone(re.search(r'^volumes:', compose, re.M), 'no named volumes: App Manager backups skip them')
        mounts = re.findall(r'^\s+- (\S+:/\S+)$', compose, re.M)
        self.assertEqual(mounts, ['${APP_DATA_DIR}/data:/data'])
        self.assertIn('DATA_DIR: /data', compose)
        for word in ('privileged', 'network_mode', 'docker.sock', 'cap_add'):
            self.assertNotIn(word, compose)

    def test_no_secrets_in_source(self):
        tracked = subprocess.run(['git', 'ls-files'], cwd=ROOT, capture_output=True, text=True).stdout.split() or \
            [str(p.relative_to(ROOT)) for p in ROOT.rglob('*') if p.is_file() and '.git' not in p.parts]
        pattern = re.compile(r'GOCSPX-(?!fake)[A-Za-z0-9_-]{20,}|ya29\.[A-Za-z0-9_-]{20,}|1//[A-Za-z0-9_-]{30,}|-----BEGIN [A-Z ]*PRIVATE KEY')
        for name in tracked:
            path = ROOT / name
            if path.suffix in ('.png', '.jpg') or not path.is_file():
                continue
            self.assertIsNone(pattern.search(path.read_text(errors='ignore')), name)

    def test_shared_style_is_the_canonical_copy(self):
        canonical = ROOT.parent.parent / 'conn-inv' / 'wizard-os' / 'style'
        if not canonical.exists():
            self.skipTest('wizard-os clone not present')
        for name in ('boot.js', 'appearance-client.js', 'tokens.css', 'ui.css'):
            self.assertEqual((ROOT / 'web' / name).read_bytes(), (canonical / name).read_bytes(), name)

    def test_wizard_md_passes_the_checker(self):
        if not CHECKER.exists():
            self.skipTest('wizard-os checker not present')
        result = subprocess.run(['/usr/bin/python3', str(CHECKER), str(ROOT)], capture_output=True, text=True)
        if 'No module named' in result.stderr:
            self.skipTest('checker needs PyYAML')
        self.assertIn('COMPATIBLE', result.stdout)
        self.assertNotIn('NOT COMPATIBLE', result.stdout)


if __name__ == '__main__':
    unittest.main()
