import queue
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from rpgds_core import EmbeddedProject
from rpgds_gui import TranslatorApp


class _Value:
    def __init__(self):
        self.value = ""

    def set(self, value):
        self.value = value


class GUIBuildModeTests(unittest.TestCase):
    def test_save_project_never_persists_an_embedded_slot(self):
        with tempfile.TemporaryDirectory() as folder:
            fake = SimpleNamespace(
                source_rom=Path(folder) / "source.nds",
                project_path=Path(folder) / "translation.rpgdsproj",
                entries=[],
                image_pngs={},
                status_var=_Value(),
                session_settings={},
                _remember_session=lambda *_args: None,
                append_log=lambda *_args: None,
            )
            with mock.patch("rpgds_gui.save_project") as save:
                TranslatorApp.save_project(fake)

            self.assertIsNone(save.call_args.args[4])

    def test_normal_compile_explicitly_uses_no_embedded_project(self):
        captured = []
        fake = SimpleNamespace(
            source_rom=Path("source.nds"),
            entries=[],
            profile=SimpleNamespace(output_name="English.nds"),
            _start_compile=lambda output, embedded: captured.append((output, embedded)),
        )
        with mock.patch(
            "rpgds_gui.filedialog.asksaveasfilename", return_value="normal.nds"
        ):
            TranslatorApp.compile(fake)

        self.assertEqual(captured, [(Path("normal.nds"), None)])

    def test_direct_boot_project_exists_only_in_one_build_job(self):
        project = EmbeddedProject(2, b"one-time-slot", "game.dsv")
        logs = []
        fake = SimpleNamespace(
            source_rom=Path("source.nds"),
            entries=[],
            image_pngs={},
            profile=SimpleNamespace(game_code=b"VEBJ"),
            worker_queue=queue.Queue(),
            status_var=_Value(),
            show_page=lambda *_args: None,
            append_log=logs.append,
        )
        fake._run_worker = lambda task, done: done(task())
        rebuilt = SimpleNamespace(
            idCode=b"VEBJ",
            filenames=SimpleNamespace(idOf=lambda _name: 0),
            files=[project.data],
            loadArm9Overlays=lambda: {},
        )
        with (
            mock.patch("rpgds_gui.compile_rom", return_value=(3, 4)) as compile_mock,
            mock.patch("rpgds_gui.ndspy.rom.NintendoDSRom.fromFile", return_value=rebuilt),
            mock.patch("rpgds_gui.messagebox.showinfo"),
        ):
            TranslatorApp._start_compile(fake, Path("direct.nds"), project)

        self.assertIs(compile_mock.call_args.args[4], project)
        self.assertFalse(hasattr(fake, "embedded_project"))
        self.assertIn("Embedded created-game slot 2", logs[0])


if __name__ == "__main__":
    unittest.main()
