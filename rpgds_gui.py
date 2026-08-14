"""Windows GUI for translating and rebuilding RPG Tsukuru DS and DS+."""

from __future__ import annotations

import io
import json
import os
import queue
import threading
import traceback
from pathlib import Path

import tkinter as tk
from tkinter import filedialog, messagebox, ttk

from PIL import Image, ImageTk
import ndspy.rom

from rpgds_core import (
    CHBG_SIZE_ALLOWANCE_PERCENT,
    EMBEDDED_PROJECT_ROM_PATH,
    EmbeddedProject,
    ImageAsset,
    TextEntry,
    auto_translate_entries,
    compile_rom,
    decode_bmbg,
    decode_chbg,
    encode_bmbg,
    embedded_project_from_slot,
    extract_text_entries,
    list_image_assets,
    load_project,
    parse_chbg,
    prepare_chbg_replacement,
    profile_for_rom,
    quick_translation,
    repair_entry_translation,
    save_project,
    scan_dsplus_project_slots,
    sanitize_import_image,
    sanitize_png_bytes,
    sha256_file,
    structural_asset_suffix,
)


APP_NAME = "RPG Tsukuru DS / DS+ Translator"


def _settings_path() -> Path:
    base = os.environ.get("LOCALAPPDATA")
    root = Path(base) if base else Path.home() / "AppData" / "Local"
    return root / "RPGDS Translator" / "settings.json"


class TranslatorApp(tk.Tk):
    def __init__(self) -> None:
        super().__init__()
        self.title(APP_NAME)
        self.geometry("1180x780")
        self.minsize(960, 640)
        self.option_add("*Font", ("Segoe UI", 9))

        self.source_rom: Path | None = None
        self.project_path: Path | None = None
        self.rom: ndspy.rom.NintendoDSRom | None = None
        self.profile = None
        self.entries: list[TextEntry] = []
        self.filtered_entries: list[TextEntry] = []
        self.images: list[ImageAsset] = []
        self.filtered_images: list[ImageAsset] = []
        self.image_pngs: dict[str, bytes] = {}
        self.embedded_project: EmbeddedProject | None = None
        self.current_image: ImageAsset | None = None
        self.preview_photo: ImageTk.PhotoImage | None = None
        self.worker_queue: queue.Queue = queue.Queue()
        self.busy = False
        self.settings_path = _settings_path()
        self.session_settings = self._read_settings()

        self._build_ui()
        self.after(100, self._poll_worker)
        self.after(250, self._auto_load_last_session)

    def _read_settings(self) -> dict[str, str]:
        try:
            data = json.loads(self.settings_path.read_text(encoding="utf-8"))
            return {str(key): str(value) for key, value in data.items() if value is not None}
        except (OSError, ValueError, TypeError):
            return {}

    def _remember_session(self, kind: str, rom_path: Path,
                          project_path: Path | None = None) -> None:
        self.session_settings["last_session"] = kind
        self.session_settings["last_rom"] = str(rom_path.resolve())
        if project_path is not None:
            self.session_settings["last_project"] = str(project_path.resolve())
        try:
            self.settings_path.parent.mkdir(parents=True, exist_ok=True)
            temporary = self.settings_path.with_suffix(".tmp")
            temporary.write_text(json.dumps(self.session_settings, indent=2), encoding="utf-8")
            temporary.replace(self.settings_path)
        except OSError as exc:
            self.append_log(f"Could not save recent-session settings: {exc}")

    def _auto_load_last_session(self) -> None:
        if self.busy or self.source_rom:
            return
        kind = self.session_settings.get("last_session", "project")
        project_value = self.session_settings.get("last_project")
        rom_value = self.session_settings.get("last_rom")

        if kind == "project" and project_value:
            project_path = Path(project_value)
            if project_path.is_file() and self._open_project_path(project_path, interactive=False):
                self.status_var.set(f"Reopening project: {project_path.name}...")
                return
        if rom_value:
            rom_path = Path(rom_value)
            if rom_path.is_file():
                self.status_var.set(f"Reopening ROM: {rom_path.name}...")
                self._load_rom(rom_path, session_kind="rom")
                return
        if kind != "project" and project_value:
            project_path = Path(project_value)
            if project_path.is_file() and self._open_project_path(project_path, interactive=False):
                self.status_var.set(f"Reopening project: {project_path.name}...")
                return
        if project_value or rom_value:
            self.append_log("Recent project/ROM was moved or deleted; automatic reopening was skipped.")

    def _build_ui(self) -> None:
        toolbar = ttk.Frame(self, padding=(8, 7))
        toolbar.pack(fill=tk.X)
        ttk.Button(toolbar, text="Open ROM", command=self.open_rom).pack(side=tk.LEFT, padx=(0, 5))
        ttk.Button(toolbar, text="Open Project", command=self.open_project).pack(side=tk.LEFT, padx=5)
        ttk.Button(toolbar, text="Save Project", command=self.save_project).pack(side=tk.LEFT, padx=5)
        self.embed_button = ttk.Button(
            toolbar, text="Embed Project from Save", command=self.embed_project_from_save,
        )
        self.embed_button.pack(side=tk.LEFT, padx=5)
        ttk.Separator(toolbar, orient=tk.VERTICAL).pack(side=tk.LEFT, fill=tk.Y, padx=8)
        self.quick_button = ttk.Button(toolbar, text="Quick Auto", command=self.quick_auto)
        self.quick_button.pack(side=tk.LEFT, padx=5)
        self.online_button = ttk.Button(toolbar, text="Auto Translate + Shorten (Online)", command=self.online_auto)
        self.online_button.pack(side=tk.LEFT, padx=5)
        self.compile_button = ttk.Button(toolbar, text="Compile ROM", command=self.compile)
        self.compile_button.pack(side=tk.RIGHT, padx=5)

        self.status_var = tk.StringVar(value="Open the original RPG Tsukuru DS ROM to begin.")
        ttk.Label(self, textvariable=self.status_var, anchor=tk.W, padding=(8, 4)).pack(fill=tk.X)
        self.session_var = tk.StringVar(value="No ROM loaded")
        ttk.Label(self, textvariable=self.session_var, anchor=tk.W, padding=(8, 0, 8, 4),
                  foreground="#555555").pack(fill=tk.X)
        self.progress = ttk.Progressbar(self, mode="determinate")
        self.progress.pack(fill=tk.X, padx=8)

        self.tabs = ttk.Notebook(self)
        self.tabs.pack(fill=tk.BOTH, expand=True, padx=8, pady=8)
        self.text_tab = ttk.Frame(self.tabs)
        self.image_tab = ttk.Frame(self.tabs)
        self.log_tab = ttk.Frame(self.tabs)
        self.tabs.add(self.text_tab, text="Text")
        self.tabs.add(self.image_tab, text="Images")
        self.tabs.add(self.log_tab, text="Build Log")
        self._build_text_tab()
        self._build_image_tab()
        self._build_log_tab()

    def _build_text_tab(self) -> None:
        controls = ttk.Frame(self.text_tab, padding=6)
        controls.pack(fill=tk.X)
        ttk.Label(controls, text="Filter:").pack(side=tk.LEFT)
        self.text_filter = tk.StringVar()
        entry = ttk.Entry(controls, textvariable=self.text_filter, width=45)
        entry.pack(side=tk.LEFT, padx=6)
        self.text_filter.trace_add("write", lambda *_: self.refresh_texts())
        self.text_summary = tk.StringVar(value="0 strings")
        ttk.Label(controls, textvariable=self.text_summary).pack(side=tk.RIGHT)

        columns = ("location", "original", "translation", "bytes", "status")
        self.text_tree = ttk.Treeview(self.text_tab, columns=columns, show="headings", selectmode="browse")
        headings = {"location": "Location", "original": "Japanese", "translation": "English",
                    "bytes": "Used / Original", "status": "Storage"}
        widths = {"location": 105, "original": 360, "translation": 360, "bytes": 105, "status": 95}
        for column in columns:
            self.text_tree.heading(column, text=headings[column])
            self.text_tree.column(column, width=widths[column], minwidth=55,
                                  stretch=column in ("original", "translation"))
        scroll = ttk.Scrollbar(self.text_tab, orient=tk.VERTICAL, command=self.text_tree.yview)
        self.text_tree.configure(yscrollcommand=scroll.set)
        scroll.pack(side=tk.RIGHT, fill=tk.Y)
        self.text_tree.pack(fill=tk.BOTH, expand=True)
        self.text_tree.bind("<<TreeviewSelect>>", self._text_selected)

        editor = ttk.LabelFrame(self.text_tab, text="Selected string", padding=8)
        editor.pack(fill=tk.X, pady=(7, 0))
        self.original_var = tk.StringVar(value="Japanese text")
        ttk.Label(editor, textvariable=self.original_var, wraplength=1060).grid(row=0, column=0, columnspan=5,
                                                                               sticky=tk.W, pady=(0, 6))
        ttk.Label(editor, text="English:").grid(row=1, column=0, sticky=tk.W)
        self.translation_entry = tk.Text(editor, height=3, wrap=tk.WORD, undo=True)
        self.translation_entry.grid(row=1, column=1, sticky=tk.EW, padx=6)
        self.translation_entry.bind("<<Modified>>", self._translation_modified)
        self.byte_var = tk.StringVar(value="0 / 0 original bytes")
        ttk.Label(editor, textvariable=self.byte_var, width=26).grid(row=1, column=2, sticky=tk.N)
        ttk.Button(editor, text="Suggest", command=self.suggest_selected).grid(row=1, column=3, padx=4,
                                                                                sticky=tk.N)
        ttk.Button(editor, text="Apply", command=self.apply_selected).grid(row=1, column=4, padx=4,
                                                                            sticky=tk.N)
        editor.columnconfigure(1, weight=1)

    def _build_image_tab(self) -> None:
        pane = ttk.Panedwindow(self.image_tab, orient=tk.HORIZONTAL)
        pane.pack(fill=tk.BOTH, expand=True)
        left = ttk.Frame(pane, padding=6)
        right = ttk.Frame(pane, padding=8)
        pane.add(left, weight=1)
        pane.add(right, weight=3)

        ttk.Label(left, text="Filter assets:").pack(anchor=tk.W)
        self.image_filter = tk.StringVar()
        ttk.Entry(left, textvariable=self.image_filter).pack(fill=tk.X, pady=(3, 6))
        self.image_filter.trace_add("write", lambda *_: self.refresh_images())
        list_frame = ttk.Frame(left)
        list_frame.pack(fill=tk.BOTH, expand=True)
        self.image_list = tk.Listbox(list_frame, exportselection=False, width=40)
        image_scroll = ttk.Scrollbar(list_frame, orient=tk.VERTICAL, command=self.image_list.yview)
        self.image_list.configure(yscrollcommand=image_scroll.set)
        self.image_list.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)
        image_scroll.pack(side=tk.RIGHT, fill=tk.Y)
        self.image_list.bind("<<ListboxSelect>>", self._image_selected)

        self.image_info = tk.StringVar(value="Select a CHBG asset")
        ttk.Label(right, textvariable=self.image_info).pack(anchor=tk.W)
        preview_frame = ttk.Frame(right, relief=tk.SUNKEN, borderwidth=1)
        preview_frame.pack(fill=tk.BOTH, expand=True, pady=8)
        self.preview_canvas = tk.Canvas(preview_frame, background="#303030", highlightthickness=0)
        self.preview_canvas.pack(fill=tk.BOTH, expand=True)
        self.preview_canvas.bind("<Configure>", lambda _event: self._draw_preview())

        buttons = ttk.Frame(right)
        buttons.pack(fill=tk.X)
        ttk.Button(buttons, text="Export PNG", command=self.export_image).pack(side=tk.LEFT, padx=4)
        ttk.Button(buttons, text="Import PNG", command=self.import_image).pack(side=tk.LEFT, padx=4)
        ttk.Button(buttons, text="Revert Image", command=self.revert_image).pack(side=tk.LEFT, padx=4)
        ttk.Label(buttons, text=(
            "Import strips metadata/unused alpha, then palette-normalizes and validates the "
            f"{100 + CHBG_SIZE_ALLOWANCE_PERCENT}% limit."
        )).pack(
            side=tk.RIGHT)

    def _build_log_tab(self) -> None:
        self.log = tk.Text(self.log_tab, wrap=tk.WORD, state=tk.DISABLED, background="#171717",
                           foreground="#e5e5e5", insertbackground="white")
        self.log.pack(fill=tk.BOTH, expand=True)

    def append_log(self, text: str) -> None:
        self.log.configure(state=tk.NORMAL)
        self.log.insert(tk.END, text.rstrip() + "\n")
        self.log.see(tk.END)
        self.log.configure(state=tk.DISABLED)

    def _set_busy(self, busy: bool, status: str = "") -> None:
        self.busy = busy
        state = tk.DISABLED if busy else tk.NORMAL
        for button in (self.quick_button, self.online_button, self.embed_button, self.compile_button):
            button.configure(state=state)
        if status:
            self.status_var.set(status)
        if not busy:
            self.progress["value"] = 0

    def _run_worker(self, task, done=None) -> None:
        if self.busy:
            return
        self._set_busy(True)

        def runner():
            try:
                result = task()
                self.worker_queue.put(("done", done, result))
            except Exception as exc:
                self.worker_queue.put(("error", exc, traceback.format_exc()))
        threading.Thread(target=runner, daemon=True).start()

    def _poll_worker(self) -> None:
        try:
            while True:
                message = self.worker_queue.get_nowait()
                if message[0] == "progress":
                    _, current, total, status = message
                    self.progress["maximum"] = max(total, 1)
                    self.progress["value"] = current
                    self.status_var.set(status)
                elif message[0] == "done":
                    _, callback, result = message
                    self._set_busy(False)
                    if callback:
                        callback(result)
                else:
                    _, exc, details = message
                    self._set_busy(False, "Operation failed.")
                    self.append_log(details)
                    messagebox.showerror(APP_NAME, str(exc))
        except queue.Empty:
            pass
        self.after(100, self._poll_worker)

    def open_rom(self) -> None:
        path = filedialog.askopenfilename(title="Open original ROM", filetypes=(("Nintendo DS ROM", "*.nds"),))
        if path:
            self._load_rom(Path(path), session_kind="rom")

    def _load_rom(self, path: Path, saved_rows: dict[str, dict] | None = None,
                  saved_images: dict[str, bytes] | None = None, session_kind: str = "rom",
                  project_path: Path | None = None,
                  saved_embedded_project: EmbeddedProject | None = None) -> None:
        def task():
            rom = ndspy.rom.NintendoDSRom.fromFile(path)
            profile = profile_for_rom(rom)
            entries = extract_text_entries(rom)
            images = list_image_assets(rom)
            return rom, profile, entries, images, sha256_file(path)

        def done(result):
            self.rom, self.profile, self.entries, self.images, digest = result
            self.source_rom = path
            self.project_path = project_path if session_kind == "project" else None
            self.image_pngs = dict(saved_images or {})
            self.embedded_project = saved_embedded_project
            if saved_rows:
                for entry in self.entries:
                    row = saved_rows.get(entry.key)
                    if row:
                        entry.translation = row.get("translation", "")
                        entry.auto = row.get("auto", "False").lower() == "true"
            self.quick_auto(silent=True)
            self.refresh_texts()
            self.refresh_images()
            self.status_var.set(f"Loaded {self.profile.title}: {len(self.entries)} strings, {len(self.images)} images")
            project_label = self.project_path.name if self.project_path else "unsaved ROM session"
            code = bytes(self.rom.idCode).decode("ascii", errors="replace")
            embedded_label = (
                f"  |  Embedded game: save slot {self.embedded_project.source_slot}"
                if self.embedded_project else ""
            )
            self.session_var.set(f"Current ROM: {path.name}  |  Game: {self.profile.title} [{code}]  |  "
                                 f"Project: {project_label}{embedded_label}")
            self.title(f"{APP_NAME} - {self.profile.title} [{code}]")
            self.append_log(f"Opened {path}\nSHA-256: {digest}\nFound {len(self.entries)} text slots and "
                            f"{len(self.images)} CHBG images.")
            self._remember_session(session_kind, path, self.project_path)
        self.status_var.set(f"Reading {path.name}...")
        self._run_worker(task, done)

    def open_project(self) -> None:
        path = filedialog.askopenfilename(title="Open translation project",
                                          filetypes=(("RPGDS project", "*.rpgdsproj"),))
        if not path:
            return
        self._open_project_path(Path(path), interactive=True)

    def _open_project_path(self, path: Path, interactive: bool) -> bool:
        try:
            source, rows, images, embedded_project = load_project(path)
        except Exception as exc:
            if interactive:
                messagebox.showerror(APP_NAME, str(exc))
            else:
                self.append_log(f"Could not reopen project {path}: {exc}")
            return False
        if not source.exists():
            remembered_rom = self.session_settings.get("last_rom")
            if remembered_rom and Path(remembered_rom).is_file():
                source = Path(remembered_rom)
            elif interactive:
                replacement = filedialog.askopenfilename(title="Locate original ROM",
                                                          filetypes=(("Nintendo DS ROM", "*.nds"),))
                if not replacement:
                    return False
                source = Path(replacement)
            else:
                self.append_log(f"Original ROM for {path.name} could not be found.")
                return False
        self._load_rom(
            source, rows, images, session_kind="project", project_path=path,
            saved_embedded_project=embedded_project,
        )
        return True

    def save_project(self) -> None:
        if not self.source_rom:
            messagebox.showinfo(APP_NAME, "Open a ROM first.")
            return
        path = self.project_path
        if path is None:
            selected = filedialog.asksaveasfilename(title="Save translation project",
                                                     defaultextension=".rpgdsproj",
                                                     filetypes=(("RPGDS project", "*.rpgdsproj"),))
            if not selected:
                return
            path = Path(selected)
        try:
            save_project(
                path, self.source_rom, self.entries, self.image_pngs, self.embedded_project,
            )
            self.project_path = path
            self._remember_session("project", self.source_rom, path)
            self.status_var.set(f"Saved project: {path.name}")
            self.append_log(f"Saved project to {path}")
        except Exception as exc:
            messagebox.showerror(APP_NAME, str(exc))

    def _refresh_embedded_project_label(self) -> None:
        base = self.session_var.get().split("  |  Embedded game:", 1)[0]
        if self.embedded_project:
            base += f"  |  Embedded game: save slot {self.embedded_project.source_slot}"
        self.session_var.set(base)

    def embed_project_from_save(self) -> None:
        if not self.rom or not self.source_rom:
            messagebox.showinfo(APP_NAME, "Open the RPG Tsukuru DS+ ROM first.")
            return
        if bytes(self.rom.idCode) != b"VEBJ":
            messagebox.showinfo(
                APP_NAME,
                "Embedded created-game projects currently support RPG Tsukuru DS+ (VEBJ) only.",
            )
            return
        selected = filedialog.askopenfilename(
            title="Open RPG Tsukuru DS+ save",
            filetypes=(
                ("RPG Tsukuru DS+ saves", "*.sav *.dsv"),
                ("Raw save", "*.sav"),
                ("DeSmuME save", "*.dsv"),
                ("All files", "*.*"),
            ),
        )
        if not selected:
            return
        save_path = Path(selected)
        try:
            slots = scan_dsplus_project_slots(save_path)
        except Exception as exc:
            messagebox.showerror(APP_NAME, str(exc))
            return

        dialog = tk.Toplevel(self)
        dialog.title("Select project to embed")
        dialog.transient(self)
        dialog.resizable(True, False)
        dialog.geometry("780x330")
        dialog.grab_set()

        ttk.Label(
            dialog,
            text=(
                f"Save: {save_path.name}\n"
                "Select a populated project whose two safety copies match. "
                "The complete slot will be stored in this translation project."
            ),
            padding=(12, 12, 12, 8),
            wraplength=740,
        ).pack(fill=tk.X)

        columns = ("slot", "status", "used", "mirror", "hash")
        tree = ttk.Treeview(dialog, columns=columns, show="headings", height=7, selectmode="browse")
        for column, heading, width in (
            ("slot", "Project", 80),
            ("status", "Status", 220),
            ("used", "Occupied copy bytes", 140),
            ("mirror", "Safety copies", 120),
            ("hash", "SHA-256", 145),
        ):
            tree.heading(column, text=heading)
            tree.column(column, width=width, anchor=tk.W, stretch=column == "status")
        tree.pack(fill=tk.BOTH, expand=True, padx=12)
        for slot in slots:
            tree.insert("", tk.END, iid=str(slot.number), values=(
                f"Slot {slot.number}", slot.status,
                f"{slot.occupied_bytes:,}" if slot.populated else "-",
                "Match" if slot.copies_match else "Differ",
                slot.sha256[:16] + "…" if slot.populated else "-",
            ))
        ready = [slot for slot in slots if slot.embeddable]
        if ready:
            tree.selection_set(str(ready[0].number))
            tree.focus(str(ready[0].number))

        result: dict[str, object] = {}

        def accept() -> None:
            selection = tree.selection()
            if not selection:
                messagebox.showinfo(APP_NAME, "Select a project slot.", parent=dialog)
                return
            slot = slots[int(selection[0]) - 1]
            if not slot.embeddable:
                messagebox.showerror(
                    APP_NAME,
                    f"Slot {slot.number} is not safe to embed.\n\n{slot.status}",
                    parent=dialog,
                )
                return
            result["project"] = embedded_project_from_slot(slot, save_path.name)
            dialog.destroy()

        def remove_existing() -> None:
            result["remove"] = True
            dialog.destroy()

        buttons = ttk.Frame(dialog, padding=12)
        buttons.pack(fill=tk.X)
        remove_button = ttk.Button(
            buttons, text="Remove Existing Embedded Project", command=remove_existing,
            state=tk.NORMAL if self.embedded_project else tk.DISABLED,
        )
        remove_button.pack(side=tk.LEFT)
        ttk.Button(buttons, text="Cancel", command=dialog.destroy).pack(side=tk.RIGHT, padx=(8, 0))
        embed_button = ttk.Button(buttons, text="Embed Selected Project", command=accept)
        embed_button.pack(side=tk.RIGHT)
        tree.bind("<Double-1>", lambda _event: accept())
        dialog.protocol("WM_DELETE_WINDOW", dialog.destroy)
        self.wait_window(dialog)

        if result.get("remove"):
            self.embedded_project = None
            self._refresh_embedded_project_label()
            self.status_var.set("Removed embedded project from this translation project.")
            self.append_log("Removed the embedded created-game project.")
            return
        project = result.get("project")
        if isinstance(project, EmbeddedProject):
            self.embedded_project = project
            self._refresh_embedded_project_label()
            self.status_var.set(
                f"Embedded project selected: {save_path.name}, slot {project.source_slot}"
            )
            self.append_log(
                f"Selected {save_path.name} slot {project.source_slot} for ROM embedding.\n"
                f"Project slot SHA-256: {project.sha256}"
            )
            messagebox.showinfo(
                APP_NAME,
                "The project slot is now attached to this translation session.\n\n"
                "Use Save Project to store it in the .rpgdsproj file. Compiling will add the "
                "slot to the ROM and install it into an empty save slot 1 on cold boot. "
                "An existing valid slot 1 is never overwritten. The compiled ROM bypasses "
                "the game logos, title screen, main menu, and project picker, then launches "
                "the embedded project through the original Play Game loader.",
            )

    def refresh_texts(self) -> None:
        query = self.text_filter.get().casefold().strip() if hasattr(self, "text_filter") else ""
        self.filtered_entries = [entry for entry in self.entries if not query or query in entry.original.casefold()
                                 or query in entry.translation.casefold() or query in entry.key.casefold()]
        self.text_tree.delete(*self.text_tree.get_children())
        for index, entry in enumerate(self.filtered_entries):
            fixed_only = bool(
                self.profile and (
                    not self.profile.allow_text_relocation
                    or entry.overlay in self.profile.fixed_slot_overlays
                )
            )
            if not entry.translation:
                status = "Pending"
            elif entry.valid:
                if entry.used_bytes > entry.max_bytes:
                    status = "TOO LONG" if fixed_only else "RELOCATE"
                else:
                    status = "In place"
            else:
                status = "Invalid"
            used = entry.used_bytes if entry.translation else 0
            original_preview = entry.original.replace("\n", " ↵ ")
            translation_preview = entry.translation.replace("\n", " ↵ ")
            location = "ARM9" if entry.overlay == -1 else f"OV{entry.overlay}"
            self.text_tree.insert("", tk.END, iid=str(index), values=(
                f"{location} 0x{entry.offset:X}", original_preview, translation_preview,
                f"{used}/{entry.max_bytes}", status))
        ready = sum(
            entry.valid and (
                not self.profile or (
                    self.profile.allow_text_relocation
                    and entry.overlay not in self.profile.fixed_slot_overlays
                )
                or entry.used_bytes <= entry.max_bytes
            )
            for entry in self.entries
        )
        self.text_summary.set(f"{len(self.filtered_entries)} shown · {ready}/{len(self.entries)} translated")

    def _selected_entry(self) -> TextEntry | None:
        selection = self.text_tree.selection()
        if not selection:
            return None
        index = int(selection[0])
        return self.filtered_entries[index] if index < len(self.filtered_entries) else None

    def _text_selected(self, _event=None) -> None:
        entry = self._selected_entry()
        if not entry:
            return
        self.original_var.set(entry.original)
        self._set_translation_text(entry.translation)
        self.translation_entry.focus_set()
        if entry.translation:
            self.translation_entry.tag_add(tk.SEL, "1.0", "end-1c")

    def _get_translation_text(self) -> str:
        return self.translation_entry.get("1.0", "end-1c")

    def _set_translation_text(self, value: str) -> None:
        self.translation_entry.delete("1.0", tk.END)
        self.translation_entry.insert("1.0", value)
        self.translation_entry.edit_modified(False)
        self._update_byte_label()

    def _translation_modified(self, _event=None) -> None:
        if not self.translation_entry.edit_modified():
            return
        self.translation_entry.edit_modified(False)
        self._update_byte_label()

    def _update_byte_label(self) -> None:
        entry = self._selected_entry()
        if not entry:
            return
        try:
            used = len(self._get_translation_text().encode("cp932"))
            if used <= entry.max_bytes:
                state = "IN PLACE"
            elif self.profile and (
                    not self.profile.allow_text_relocation
                    or entry.overlay in self.profile.fixed_slot_overlays):
                state = "TOO LONG"
            else:
                state = "RELOCATE"
        except UnicodeEncodeError:
            used, state = -1, "UNSUPPORTED CHAR"
        self.byte_var.set(f"{used} / {entry.max_bytes} original · {state}")

    def apply_selected(self) -> None:
        entry = self._selected_entry()
        if not entry:
            return
        value = self._get_translation_text().strip()
        if value:
            repaired = repair_entry_translation(entry, value)
            if not repaired:
                if structural_asset_suffix(entry.original):
                    detail = (
                        f"This catalog label must reserve room for its required "
                        f"{structural_asset_suffix(entry.original)} asset ID."
                    )
                else:
                    detail = (
                        "This runtime message must fit its original slot and use the "
                        "full-width CP932 event charset."
                    )
                messagebox.showerror(APP_NAME, detail)
                return
            value = repaired
            self._set_translation_text(value)
        previous_value = entry.translation
        entry.translation = value
        if value and not entry.valid:
            entry.translation = previous_value
            messagebox.showerror(APP_NAME, "The text contains unsupported characters or changes "
                                              "a required format token.")
            return
        if (value and self.profile
                and (not self.profile.allow_text_relocation
                     or entry.overlay in self.profile.fixed_slot_overlays)
                and len(value.encode("cp932")) > entry.max_bytes):
            entry.translation = previous_value
            messagebox.showerror(
                APP_NAME,
                "DS+ stability mode keeps every string in its original byte slot. "
                f"Shorten this translation to {entry.max_bytes} bytes or fewer.",
            )
            return
        entry.auto = False
        self.refresh_texts()

    def suggest_selected(self) -> None:
        entry = self._selected_entry()
        if not entry:
            return
        suggestion = quick_translation(entry)
        if suggestion:
            self._set_translation_text(suggestion)
        else:
            messagebox.showinfo(APP_NAME, "No offline suggestion is available. Use Auto Translate All (Online).")

    def quick_auto(self, silent: bool = False) -> None:
        if not self.entries:
            if not silent:
                messagebox.showinfo(APP_NAME, "Open a ROM first.")
            return
        completed, _ = auto_translate_entries(self.entries, online=False)
        self.refresh_texts()
        if not silent:
            self.status_var.set(f"Added {completed} safe offline translations.")

    def online_auto(self) -> None:
        if not self.entries:
            messagebox.showinfo(APP_NAME, "Open a ROM first.")
            return
        pending = [entry for entry in self.entries if not entry.translation]
        if not pending:
            messagebox.showinfo(APP_NAME, "Every extracted string already has a translation.")
            return
        if not messagebox.askyesno(APP_NAME, f"Translate and aggressively shorten {len(pending)} pending strings?\n\n"
                                            "The tool will reduce sentences to short UI labels and RPG-style "
                                            "codes where space is extremely tight. Required tokens and negative "
                                            "meaning are preserved. Review auto translations before release."):
            return

        def progress(current, total):
            self.worker_queue.put(("progress", current, total, f"Auto translating {current}/{total}..."))

        def task():
            return auto_translate_entries(pending, progress=progress, online=True)

        def done(result):
            completed, skipped = result
            self.refresh_texts()
            self.status_var.set(f"Auto translation complete: {completed} added, {skipped} need manual editing.")
            self.append_log(f"Online auto translation added {completed} fitted strings; {skipped} did not fit.")
        self.status_var.set("Starting online translation...")
        self._run_worker(task, done)

    def refresh_images(self) -> None:
        query = self.image_filter.get().casefold().strip() if hasattr(self, "image_filter") else ""
        self.filtered_images = [asset for asset in self.images if not query or query in asset.name.casefold()]
        self.image_list.delete(0, tk.END)
        for asset in self.filtered_images:
            changed = " *" if asset.name in self.image_pngs else ""
            self.image_list.insert(tk.END, asset.name + changed)

    def _asset_palette(self, asset: ImageAsset):
        if asset.palette_file_id is None or not self.rom:
            return None
        raw = bytes(self.rom.files[asset.palette_file_id])
        name = self.rom.filenames.filenameOf(asset.palette_file_id)
        return parse_chbg(raw, name.lower().endswith(".blz")).palette

    def _decode_asset(self, asset: ImageAsset, raw: bytes):
        if asset.kind == "BMBG":
            return decode_bmbg(raw, asset.compressed, self._asset_palette(asset))
        return decode_chbg(raw, asset.compressed)

    def _image_selected(self, _event=None) -> None:
        selection = self.image_list.curselection()
        if not selection or not self.rom:
            return
        asset = self.filtered_images[selection[0]]
        self.current_image = asset
        try:
            if asset.name in self.image_pngs:
                with Image.open(io.BytesIO(self.image_pngs[asset.name])) as source_image:
                    image = sanitize_import_image(source_image).convert("RGB")
                changed = " · replacement loaded"
            else:
                image = self._decode_asset(asset, bytes(self.rom.files[asset.file_id]))
                changed = ""
            self.preview_image = image
            storage = f"{asset.tile_count} tiles" if asset.kind == "CHBG" else "linear bitmap"
            self.image_info.set(f"{asset.name} · {asset.kind} · {asset.width}×{asset.height} · {asset.bpp}bpp · "
                                f"{asset.colors} palette colors · {storage} · "
                                f"{asset.decompressed_size:,} decoded bytes{changed}")
            self._draw_preview()
        except Exception as exc:
            messagebox.showerror(APP_NAME, str(exc))

    def _draw_preview(self) -> None:
        image = getattr(self, "preview_image", None)
        if image is None:
            return
        width = max(self.preview_canvas.winfo_width() - 20, 1)
        height = max(self.preview_canvas.winfo_height() - 20, 1)
        scale = min(width / image.width, height / image.height, 4.0)
        size = (max(1, int(image.width * scale)), max(1, int(image.height * scale)))
        resized = image.resize(size, Image.Resampling.NEAREST)
        self.preview_photo = ImageTk.PhotoImage(resized)
        self.preview_canvas.delete("all")
        self.preview_canvas.create_image(self.preview_canvas.winfo_width() // 2,
                                         self.preview_canvas.winfo_height() // 2,
                                         image=self.preview_photo, anchor=tk.CENTER)

    def export_image(self) -> None:
        if not self.current_image or not self.rom:
            return
        path = filedialog.asksaveasfilename(title="Export image", defaultextension=".png",
                                            initialfile=Path(self.current_image.name).stem + ".png",
                                            filetypes=(("PNG image", "*.png"),))
        if not path:
            return
        if self.current_image.name in self.image_pngs:
            Path(path).write_bytes(sanitize_png_bytes(
                self.image_pngs[self.current_image.name],
            ))
        else:
            self._decode_asset(self.current_image,
                               bytes(self.rom.files[self.current_image.file_id])).save(path, "PNG")
        self.status_var.set(f"Exported {Path(path).name}")

    def import_image(self) -> None:
        if not self.current_image:
            return
        path = filedialog.askopenfilename(title="Import replacement PNG", filetypes=(("PNG image", "*.png"),))
        if not path:
            return
        try:
            png = Path(path).read_bytes()
            with Image.open(io.BytesIO(png)) as source_image:
                metadata_removed = bool(source_image.info)
                try:
                    metadata_removed = metadata_removed or bool(source_image.getexif())
                except (AttributeError, OSError, ValueError):
                    pass
                image = sanitize_import_image(source_image)
            if image.size != (self.current_image.width, self.current_image.height):
                raise ValueError(f"Replacement must be exactly {self.current_image.width}×"
                                 f"{self.current_image.height} pixels.")
            original = bytes(self.rom.files[self.current_image.file_id])
            if self.current_image.kind == "BMBG":
                encoded = encode_bmbg(image, original, self.current_image.compressed,
                                       self._asset_palette(self.current_image))
                normalized = decode_bmbg(encoded, self.current_image.compressed,
                                          self._asset_palette(self.current_image))
                output = io.BytesIO()
                normalized.convert("RGB").save(output, "PNG", optimize=True, compress_level=9)
                self.image_pngs[self.current_image.name] = output.getvalue()
                self.preview_image = normalized
                self.refresh_images()
                self._draw_preview()
                self.image_info.set(
                    f"{self.current_image.name} · BMBG · {self.current_image.width}×"
                    f"{self.current_image.height} · {self.current_image.bpp}bpp · "
                    f"{self.current_image.colors} palette colors · fixed "
                    f"{self.current_image.decompressed_size:,} decoded bytes"
                )
                self.status_var.set(
                    f"Imported BMBG safely at its original fixed decoded size: "
                    f"{self.current_image.decompressed_size:,} bytes"
                )
                self.append_log(f"Imported {self.current_image.name} as a fixed-size BMBG bitmap.")
                return
            prepared = prepare_chbg_replacement(
                image.convert("RGBA"), original, self.current_image.compressed,
                self.current_image.name.lower() == "wifi/castle-logo.bin",
            )
            normalized = decode_chbg(prepared.data, self.current_image.compressed)
            output = io.BytesIO()
            # Store exact palette-normalized RGB pixels with no EXIF, XMP,
            # IPTC, ICC, DPI, software, comment, or redundant alpha data.
            clean_normalized = Image.frombytes(
                "RGB", normalized.size, normalized.convert("RGB").tobytes(),
            )
            clean_normalized.save(output, "PNG", optimize=True, compress_level=9)
            self.image_pngs[self.current_image.name] = output.getvalue()
            self.preview_image = normalized
            self.refresh_images()
            self._draw_preview()
            palette_note = (
                f"; {prepared.palette_adjusted_pixels:,} pixels matched to the original palette"
                if prepared.palette_adjusted_pixels else ""
            )
            cleanup_note = (
                "; source metadata removed and PNG normalized to 8-bit RGB"
                if metadata_removed
                else "; PNG normalized to metadata-free 8-bit RGB"
            )
            maximum_decoded = (
                prepared.original_decompressed_size
                * (100 + CHBG_SIZE_ALLOWANCE_PERCENT) // 100
            )
            self.image_info.set(
                f"{self.current_image.name} · {self.current_image.width}×{self.current_image.height} · "
                f"{self.current_image.bpp}bpp · {self.current_image.colors} palette colors · "
                f"{prepared.output_tiles} tiles (original {prepared.original_tiles}, "
                f"max {prepared.capacity_tiles})"
            )
            self.status_var.set(
                f"Imported safely: {prepared.output_decompressed_size:,}/"
                f"{maximum_decoded:,} allowed decoded bytes{palette_note}{cleanup_note}"
            )
            self.append_log(
                f"Imported {self.current_image.name}: {prepared.output_tiles} tiles, "
                f"{prepared.output_decompressed_size:,} decoded bytes "
                f"(original {prepared.original_tiles} tiles / "
                f"{prepared.original_decompressed_size:,} bytes; "
                f"hard limit +{CHBG_SIZE_ALLOWANCE_PERCENT}% = {maximum_decoded:,} bytes)"
                f"{palette_note}{cleanup_note}."
            )
        except Exception as exc:
            messagebox.showerror(APP_NAME, f"{self.current_image.name}\n\n{exc}")

    def revert_image(self) -> None:
        if not self.current_image or not self.rom:
            return
        self.image_pngs.pop(self.current_image.name, None)
        self.preview_image = self._decode_asset(
            self.current_image, bytes(self.rom.files[self.current_image.file_id]),
        )
        self.refresh_images()
        self._draw_preview()

    def compile(self) -> None:
        if not self.source_rom:
            messagebox.showinfo(APP_NAME, "Open a ROM first.")
            return
        invalid = [entry for entry in self.entries if entry.translation and not entry.valid]
        if invalid:
            messagebox.showerror(APP_NAME, f"Fix {len(invalid)} invalid translations before compiling.")
            return
        output = filedialog.asksaveasfilename(title="Compile translated ROM", defaultextension=".nds",
                                              initialfile=self.profile.output_name,
                                              filetypes=(("Nintendo DS ROM", "*.nds"),))
        if not output:
            return
        output_path = Path(output)

        def progress(current, total, status):
            self.worker_queue.put(("progress", current, total, status))

        def task():
            progress(1, 3, "Applying text and image replacements...")
            result = compile_rom(
                self.source_rom, output_path, self.entries, self.image_pngs,
                self.embedded_project,
            )
            progress(2, 3, "Verifying rebuilt ROM...")
            rebuilt = ndspy.rom.NintendoDSRom.fromFile(output_path)
            rebuilt.loadArm9Overlays()
            if bytes(rebuilt.idCode) != self.profile.game_code:
                raise ValueError("Rebuilt ROM verification failed")
            if self.embedded_project:
                embedded_id = rebuilt.filenames.idOf(EMBEDDED_PROJECT_ROM_PATH)
                if embedded_id is None:
                    raise ValueError("Rebuilt ROM is missing the embedded project asset")
                if bytes(rebuilt.files[embedded_id]) != self.embedded_project.data:
                    raise ValueError("Rebuilt ROM embedded-project verification failed")
            progress(3, 3, "Compilation complete.")
            return result

        def done(result):
            text_count, image_count = result
            self.status_var.set(f"Compiled {output_path.name}")
            self.append_log(f"Compiled {output_path}\nApplied {text_count} text translations and "
                            f"{image_count} image replacements. Rebuilt ROM parsed successfully."
                            + (
                                f"\nEmbedded created-game slot {self.embedded_project.source_slot} "
                                f"as {EMBEDDED_PROJECT_ROM_PATH}; cold-boot installer enabled."
                                if self.embedded_project else ""
                            ))
            self.tabs.select(self.log_tab)
            messagebox.showinfo(APP_NAME, f"ROM compiled successfully.\n\n{output_path}\n\n"
                                          f"Text changes: {text_count}\nImage changes: {image_count}")
        self.status_var.set("Compiling ROM...")
        self._run_worker(task, done)


def main() -> None:
    app = TranslatorApp()
    app.mainloop()


if __name__ == "__main__":
    main()
