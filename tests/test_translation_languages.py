import io
import json
import unittest
from unittest import mock

from rpgds_core import (
    EXACT_TRANSLATIONS,
    TextEntry,
    _translate_request,
    auto_translate_entries,
)


class TranslationLanguageTests(unittest.TestCase):
    def test_google_request_uses_selected_target_language(self) -> None:
        response = io.BytesIO(json.dumps([[['Hola', None, None, None]]]).encode("utf-8"))
        with mock.patch("rpgds_core.urllib.request.urlopen", return_value=response) as urlopen:
            result = _translate_request(["こんにちは"], "es")

        self.assertEqual(result, ["Hola"])
        self.assertIn("&tl=es&", urlopen.call_args.args[0].full_url)

    def test_invalid_target_language_is_rejected_before_request(self) -> None:
        with self.assertRaisesRegex(ValueError, "Unsupported Google Translate"):
            _translate_request(["こんにちは"], "../../bad")

    def test_non_english_target_does_not_apply_english_glossary(self) -> None:
        original = next(iter(EXACT_TRANSLATIONS))
        entry = TextEntry(-1, 0, 0x02000000, 200, original)

        completed, remaining = auto_translate_entries(
            [entry], online=False, target_language="es",
        )

        self.assertEqual((completed, remaining), (0, 1))
        self.assertEqual(entry.translation, "")


if __name__ == "__main__":
    unittest.main()
