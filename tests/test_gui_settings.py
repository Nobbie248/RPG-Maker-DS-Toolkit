import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import rpgds_gui


class GUISettingsTests(unittest.TestCase):
    def test_legacy_settings_are_loaded_and_migrated(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            legacy_path = root / "RPGDS Translator" / "settings.json"
            current_path = root / "RPG Maker DS Toolkit" / "settings.json"
            legacy_path.parent.mkdir(parents=True)
            expected = {"last_session": "project", "last_rom": "example.nds"}
            legacy_path.write_text(json.dumps(expected), encoding="utf-8")
            app = SimpleNamespace(settings_path=current_path)

            with patch.object(rpgds_gui, "_legacy_settings_path", return_value=legacy_path):
                actual = rpgds_gui.TranslatorApp._read_settings(app)

            self.assertEqual(actual, expected)
            self.assertEqual(json.loads(current_path.read_text(encoding="utf-8")), expected)


if __name__ == "__main__":
    unittest.main()
