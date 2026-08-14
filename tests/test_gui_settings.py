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

    def test_page_navigation_raises_page_and_updates_title(self) -> None:
        class Page:
            raised = False

            def tkraise(self) -> None:
                self.raised = True

        class Value:
            value = ""

            def set(self, value: str) -> None:
                self.value = value

        page = Page()
        title = Value()
        app = SimpleNamespace(pages={"graphics": page}, page_title_var=title)

        rpgds_gui.TranslatorApp.show_page(app, "graphics")

        self.assertTrue(page.raised)
        self.assertEqual(title.value, "Graphics Studio")

    def test_selected_translation_language_maps_to_google_code(self) -> None:
        app = SimpleNamespace(target_language_var=SimpleNamespace(get=lambda: "Spanish"))

        self.assertEqual(rpgds_gui.TranslatorApp._target_language_code(app), "es")


if __name__ == "__main__":
    unittest.main()
