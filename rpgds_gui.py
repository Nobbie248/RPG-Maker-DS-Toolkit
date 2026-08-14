"""Windows toolkit for translating, modifying, and rebuilding RPG Tsukuru DS games."""

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
import ndspy.soundArchive
import ndspy.soundSequence
import mido

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
    load_project_audio,
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
from rpgds_audio import (
    SDAT_ROM_PATH,
    export_audio_workspace,
    list_audio_assets,
    midi_to_sequence,
    midi_track_names,
    play_pcm_bytes,
    render_sequence_ncsf_pcm,
    safe_filename,
    sequence_for_asset,
    sequence_to_midi,
    stop_audio,
)


APP_NAME = "RPG Maker DS Toolkit"
UI_BG = "#0b0f14"
UI_PANEL = "#10161d"
UI_CONTROL = "#1b2530"
UI_CONTROL_HOVER = "#243442"
UI_BORDER = "#324553"
UI_TEXT = "#e6edf3"
UI_MUTED = "#91a4b2"
UI_BLUE = "#38bdf8"
UI_GREEN = "#2dd4bf"
UI_ACTIVE = "#0f766e"
TRANSLATION_LANGUAGES = {
    "English": "en",
    "Spanish": "es",
    "French": "fr",
    "German": "de",
    "Italian": "it",
    "Portuguese": "pt",
    "Dutch": "nl",
    "Polish": "pl",
    "Turkish": "tr",
    "Indonesian": "id",
}


def _settings_path() -> Path:
    base = os.environ.get("LOCALAPPDATA")
    root = Path(base) if base else Path.home() / "AppData" / "Local"
    return root / "RPG Maker DS Toolkit" / "settings.json"


def _legacy_settings_path() -> Path:
    base = os.environ.get("LOCALAPPDATA")
    root = Path(base) if base else Path.home() / "AppData" / "Local"
    return root / "RPGDS Translator" / "settings.json"


class TranslatorApp(tk.Tk):
    def __init__(self) -> None:
        super().__init__()
        self.title(APP_NAME)
        self.overrideredirect(True)
        self._drag_origin: tuple[int, int] | None = None
        self._normal_geometry: str | None = None
        self._is_maximized = False
        screen_width = self.winfo_screenwidth()
        screen_height = self.winfo_screenheight()
        window_width = min(1180, max(820, screen_width - 80))
        window_height = min(780, max(600, screen_height - 100))
        self.geometry(f"{window_width}x{window_height}")
        self.minsize(min(900, window_width), min(620, window_height))
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
        self.audio_replacements: dict[str, bytes] = {}
        self.sdat = None
        self.audio_assets = []
        self.filtered_audio_assets = []
        self.current_audio = None
        self.current_image: ImageAsset | None = None
        self.preview_photo: ImageTk.PhotoImage | None = None
        self.worker_queue: queue.Queue = queue.Queue()
        self.busy = False
        self.settings_path = _settings_path()
        self.session_settings = self._read_settings()

        self._build_ui()
        self.after(100, self._poll_worker)
        self.after(250, self._auto_load_last_session)

    def _build_window_caption(self) -> None:
        caption = tk.Frame(self, background="#000000", height=31)
        caption.pack(fill=tk.X)
        caption.pack_propagate(False)
        title = tk.Label(
            caption, text=APP_NAME, background="#000000", foreground=UI_TEXT,
            font=("Segoe UI", 9), anchor=tk.W, padx=12,
        )
        title.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)
        self.maximize_button = tk.Button(
            caption, text="□", command=self._toggle_maximize,
            background="#000000", foreground=UI_TEXT,
            activebackground=UI_CONTROL_HOVER, activeforeground="white",
            relief=tk.FLAT, borderwidth=0, width=6, font=("Segoe UI", 10),
        )
        close_button = tk.Button(
            caption, text="✕", command=self.destroy,
            background="#000000", foreground=UI_TEXT,
            activebackground="#c42b1c", activeforeground="white",
            relief=tk.FLAT, borderwidth=0, width=6, font=("Segoe UI", 10),
        )
        minimize_button = tk.Button(
            caption, text="—", command=self._minimize_window,
            background="#000000", foreground=UI_TEXT,
            activebackground=UI_CONTROL_HOVER, activeforeground="white",
            relief=tk.FLAT, borderwidth=0, width=6, font=("Segoe UI", 10),
        )
        close_button.pack(side=tk.RIGHT, fill=tk.Y)
        self.maximize_button.pack(side=tk.RIGHT, fill=tk.Y)
        minimize_button.pack(side=tk.RIGHT, fill=tk.Y)
        for widget in (caption, title):
            widget.bind("<ButtonPress-1>", self._start_window_drag)
            widget.bind("<B1-Motion>", self._drag_window)
            widget.bind("<Double-Button-1>", lambda _event: self._toggle_maximize())

    def _start_window_drag(self, event: tk.Event) -> None:
        if self._is_maximized:
            return
        self._drag_origin = (event.x_root - self.winfo_x(), event.y_root - self.winfo_y())

    def _drag_window(self, event: tk.Event) -> None:
        if self._drag_origin is None or self._is_maximized:
            return
        offset_x, offset_y = self._drag_origin
        self.geometry(f"+{event.x_root - offset_x}+{event.y_root - offset_y}")

    def _toggle_maximize(self) -> None:
        if self._is_maximized:
            if self._normal_geometry:
                self.geometry(self._normal_geometry)
            self._is_maximized = False
            self.maximize_button.configure(text="□")
            return
        self._normal_geometry = self.geometry()
        left = top = 0
        width = self.winfo_screenwidth()
        height = self.winfo_screenheight()
        if os.name == "nt":
            try:
                import ctypes

                class Rect(ctypes.Structure):
                    _fields_ = (("left", ctypes.c_long), ("top", ctypes.c_long),
                                ("right", ctypes.c_long), ("bottom", ctypes.c_long))

                work_area = Rect()
                if ctypes.windll.user32.SystemParametersInfoW(
                    0x0030, 0, ctypes.byref(work_area), 0,
                ):
                    left, top = work_area.left, work_area.top
                    width = work_area.right - work_area.left
                    height = work_area.bottom - work_area.top
            except (AttributeError, OSError):
                pass
        self.geometry(f"{width}x{height}+{left}+{top}")
        self._is_maximized = True
        self.maximize_button.configure(text="❐")

    def _minimize_window(self) -> None:
        self.overrideredirect(False)
        self.iconify()
        self.after(100, self._restore_borderless_after_minimize)

    def _restore_borderless_after_minimize(self) -> None:
        if self.state() == "iconic":
            self.after(100, self._restore_borderless_after_minimize)
            return
        self.overrideredirect(True)

    def _read_settings(self) -> dict[str, str]:
        for path in (self.settings_path, _legacy_settings_path()):
            try:
                data = json.loads(path.read_text(encoding="utf-8"))
                settings = {str(key): str(value) for key, value in data.items() if value is not None}
                if path != self.settings_path:
                    self.settings_path.parent.mkdir(parents=True, exist_ok=True)
                    temporary = self.settings_path.with_suffix(".tmp")
                    temporary.write_text(json.dumps(settings, indent=2), encoding="utf-8")
                    temporary.replace(self.settings_path)
                return settings
            except (OSError, ValueError, TypeError):
                continue
        return {}

    def _remember_session(self, kind: str, rom_path: Path,
                          project_path: Path | None = None) -> None:
        self.session_settings["last_session"] = kind
        self.session_settings["last_rom"] = str(rom_path.resolve())
        if project_path is not None:
            self.session_settings["last_project"] = str(project_path.resolve())
        self._persist_session_settings()

    def _persist_session_settings(self) -> None:
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
        self.configure(background=UI_BG)
        self._configure_styles()
        self.status_var = tk.StringVar(value="Open the original RPG Tsukuru DS ROM to begin.")
        self.session_var = tk.StringVar(value="No original ROM loaded")
        self._build_window_caption()

        titlebar = tk.Frame(self, background=UI_BG, padx=16, pady=8)
        titlebar.pack(fill=tk.X)
        tk.Label(
            titlebar, text=APP_NAME, background=UI_BG, foreground=UI_TEXT,
            font=("Consolas", 13, "bold"), anchor=tk.W,
        ).pack(side=tk.LEFT)
        self.page_title_var = tk.StringVar(value="Dashboard")
        tk.Label(
            titlebar, textvariable=self.page_title_var, background=UI_BG,
            foreground=UI_GREEN, font=("Consolas", 9), anchor=tk.E,
        ).pack(side=tk.RIGHT)

        nav_border = tk.Frame(self, background=UI_BORDER, height=1)
        nav_border.pack(fill=tk.X, padx=14)
        nav = tk.Frame(self, background=UI_BG, padx=14, pady=7)
        nav.pack(fill=tk.X)
        self.nav_buttons: dict[str, tk.Button] = {}
        nav_items = (
            ("edit", "File"), ("text", "Text"),
            ("graphics", "Graphics"), ("compile", "Compile"),
            ("direct", "Direct Boot"), ("audio", "Music / SFX"),
        )
        for name, label in nav_items:
            button = tk.Button(
                nav, text=label, command=lambda target=name: self.show_page(target),
                background=UI_CONTROL, foreground=UI_TEXT, activebackground=UI_CONTROL_HOVER,
                activeforeground="white", relief=tk.FLAT, borderwidth=0,
                highlightthickness=1, highlightbackground=UI_BORDER,
                font=("Consolas", 9), padx=15, pady=6, cursor="hand2",
            )
            button.pack(side=tk.LEFT, padx=(0, 2))
            self.nav_buttons[name] = button

        session_bar = tk.Frame(self, background=UI_PANEL, padx=18, pady=7)
        session_bar.pack(fill=tk.X)
        tk.Label(
            session_bar, textvariable=self.session_var, background=UI_PANEL,
            foreground=UI_MUTED, font=("Consolas", 9), anchor=tk.W,
        ).pack(fill=tk.X)

        self.page_container = tk.Frame(self, background=UI_BG)
        self.page_container.pack(fill=tk.BOTH, expand=True)
        self.page_container.grid_rowconfigure(0, weight=1)
        self.page_container.grid_columnconfigure(0, weight=1)
        self.pages: dict[str, tk.Widget] = {}
        for name in ("edit", "text", "graphics", "compile", "direct", "audio"):
            page = tk.Frame(self.page_container, background=UI_BG)
            page.grid(row=0, column=0, sticky="nsew")
            self.pages[name] = page

        self.text_tab = self.pages["text"]
        self.image_tab = self.pages["graphics"]
        self.log_tab = self.pages["compile"]
        self._build_edit_page()
        self._build_text_tab()
        self._build_image_tab()
        self._build_compile_page()
        self._build_direct_boot_page()
        self._build_audio_page()

        footer = tk.Frame(self, background=UI_BG, padx=16, pady=7)
        footer.pack(fill=tk.X, side=tk.BOTTOM)
        tk.Frame(footer, background=UI_BORDER, height=1).pack(fill=tk.X, pady=(0, 7))
        tk.Label(
            footer, textvariable=self.status_var, background=UI_BG, foreground=UI_MUTED,
            font=("Consolas", 9), anchor=tk.W,
        ).pack(fill=tk.X)
        self.progress = ttk.Progressbar(footer, mode="determinate", style="Toolkit.Horizontal.TProgressbar")
        self.progress.pack(fill=tk.X, pady=(5, 0))
        self.show_page("edit")

    def _configure_styles(self) -> None:
        style = ttk.Style(self)
        if "clam" in style.theme_names():
            style.theme_use("clam")
        style.configure(".", background=UI_BG, foreground=UI_TEXT, font=("Consolas", 9))
        style.configure("TFrame", background=UI_BG)
        style.configure("TLabel", background=UI_PANEL, foreground=UI_TEXT)
        style.configure("TPanedwindow", background=UI_BG)
        style.configure(
            "TButton", background=UI_CONTROL, foreground=UI_TEXT,
            font=("Consolas", 9), padding=(10, 6), borderwidth=1,
        )
        style.map("TButton", background=[("active", UI_CONTROL_HOVER), ("disabled", "#263640")])
        style.configure("Toolkit.TFrame", background=UI_PANEL)
        style.configure("Toolkit.TLabelframe", background=UI_PANEL, bordercolor=UI_BORDER)
        style.configure(
            "Toolkit.TLabelframe.Label", background=UI_PANEL, foreground=UI_GREEN,
            font=("Consolas", 10, "bold"),
        )
        style.configure(
            "Primary.TButton", background="#155e75", foreground="white",
            font=("Consolas", 10, "bold"), padding=(14, 9), borderwidth=1,
        )
        style.map("Primary.TButton", background=[("active", "#0e7490"), ("disabled", "#263640")])
        style.configure(
            "Secondary.TButton", background=UI_CONTROL, foreground=UI_TEXT,
            font=("Consolas", 9), padding=(11, 7), borderwidth=1,
        )
        style.map("Secondary.TButton", background=[("active", UI_CONTROL_HOVER)])
        style.configure(
            "Treeview", rowheight=28, font=("Consolas", 9), background=UI_PANEL,
            foreground=UI_TEXT, fieldbackground=UI_PANEL, bordercolor=UI_BORDER,
        )
        style.map("Treeview", background=[("selected", UI_ACTIVE)], foreground=[("selected", "white")])
        style.configure(
            "Treeview.Heading", font=("Consolas", 9, "bold"), padding=(6, 7),
            background=UI_CONTROL, foreground=UI_TEXT, bordercolor=UI_BORDER,
        )
        style.configure("TEntry", fieldbackground=UI_CONTROL, foreground=UI_TEXT, insertcolor=UI_TEXT)
        style.configure(
            "TCombobox", fieldbackground=UI_CONTROL, background=UI_CONTROL,
            foreground=UI_TEXT, arrowcolor=UI_GREEN, bordercolor=UI_BORDER,
        )
        style.map(
            "TCombobox", fieldbackground=[("readonly", UI_CONTROL)],
            foreground=[("readonly", UI_TEXT)], selectbackground=[("readonly", UI_CONTROL)],
            selectforeground=[("readonly", UI_TEXT)],
        )
        style.configure(
            "Toolkit.Horizontal.TProgressbar", troughcolor=UI_CONTROL,
            background=UI_GREEN, bordercolor=UI_CONTROL, lightcolor=UI_GREEN,
            darkcolor=UI_GREEN,
        )

    def show_page(self, name: str) -> None:
        page = self.pages.get(name)
        if page is None:
            raise ValueError(f"Unknown application page: {name}")
        titles = {
            "edit": "File", "text": "Text & Translation",
            "graphics": "Graphics Studio", "compile": "Compile & Build Log",
            "direct": "Direct-Boot Builder", "audio": "Music & Sound Effects",
        }
        self.page_title_var.set(titles[name])
        for page_name, button in getattr(self, "nav_buttons", {}).items():
            active = page_name == name
            button.configure(
                background=UI_ACTIVE if active else UI_CONTROL,
                foreground="white" if active else UI_TEXT,
                highlightbackground=UI_GREEN if active else UI_BORDER,
            )
        page.tkraise()

    def _page_heading(self, parent: tk.Widget, title: str, subtitle: str, color: str) -> tk.Frame:
        banner = tk.Frame(
            parent, background=UI_PANEL, padx=18, pady=13,
            highlightthickness=1, highlightbackground=UI_BORDER,
        )
        banner.pack(fill=tk.X, padx=20, pady=(14, 10))
        tk.Label(
            banner, text=title, background=UI_PANEL, foreground=color,
            font=("Consolas", 13, "bold"), anchor=tk.W,
        ).pack(anchor=tk.W)
        tk.Label(
            banner, text=subtitle, background=UI_PANEL, foreground=UI_MUTED,
            font=("Consolas", 9), anchor=tk.W, justify=tk.LEFT,
        ).pack(anchor=tk.W, pady=(3, 0))
        return banner

    def _build_edit_page(self) -> None:
        page = self.pages["edit"]
        self._page_heading(
            page, "File", "Open source material, resume a toolkit project, or save your current work.",
            UI_BLUE,
        )
        body = tk.Frame(page, background=UI_BG)
        body.pack(fill=tk.BOTH, expand=True, padx=20, pady=(0, 20))
        actions = tk.Frame(
            body, background=UI_PANEL, padx=18, pady=18,
            highlightthickness=1, highlightbackground=UI_BORDER,
        )
        actions.pack(fill=tk.X)
        tk.Label(
            actions, text="Project Files", background=UI_PANEL, foreground=UI_GREEN,
            font=("Consolas", 10, "bold"),
        ).pack(anchor=tk.W, pady=(0, 12))
        row = tk.Frame(actions, background=UI_PANEL)
        row.pack(fill=tk.X)
        ttk.Button(row, text="Open Original ROM", command=self.open_rom, style="Primary.TButton").pack(side=tk.LEFT, padx=(0, 8))
        ttk.Button(row, text="Open Toolkit Project", command=self.open_project, style="Secondary.TButton").pack(side=tk.LEFT, padx=8)
        ttk.Button(row, text="Save Toolkit Project", command=self.save_project, style="Secondary.TButton").pack(side=tk.LEFT, padx=8)
        self.project_save_var = tk.StringVar(value="")
        tk.Label(
            row, textvariable=self.project_save_var, background=UI_PANEL,
            foreground=UI_GREEN, font=("Consolas", 9, "bold"),
        ).pack(side=tk.LEFT, padx=(14, 0))

    def _build_text_tab(self) -> None:
        self._page_heading(
            self.text_tab, "Text & Translation",
            "Search every exposed string, apply translations, and keep byte-sensitive game text safe.",
            UI_GREEN,
        )
        controls = ttk.Frame(self.text_tab, padding=(20, 6), style="Toolkit.TFrame")
        controls.pack(fill=tk.X, padx=20)
        filter_row = ttk.Frame(controls, style="Toolkit.TFrame")
        filter_row.pack(fill=tk.X)
        ttk.Label(filter_row, text="Filter:").pack(side=tk.LEFT)
        self.text_filter = tk.StringVar()
        entry = ttk.Entry(filter_row, textvariable=self.text_filter, width=45)
        entry.pack(side=tk.LEFT, padx=6)
        self.text_filter.trace_add("write", lambda *_: self.refresh_texts())
        self.text_summary = tk.StringVar(value="0 strings")
        ttk.Label(filter_row, textvariable=self.text_summary).pack(side=tk.RIGHT)

        auto_row = ttk.Frame(controls, style="Toolkit.TFrame")
        auto_row.pack(fill=tk.X, pady=(8, 0))
        ttk.Label(auto_row, text="Google Translate target:").pack(side=tk.LEFT)
        saved_language = self.session_settings.get("translation_language", "en")
        selected_name = next(
            (name for name, code in TRANSLATION_LANGUAGES.items() if code == saved_language),
            "English",
        )
        self.target_language_var = tk.StringVar(value=selected_name)
        self.target_language_combo = ttk.Combobox(
            auto_row, textvariable=self.target_language_var,
            values=tuple(TRANSLATION_LANGUAGES), state="readonly", width=16,
        )
        self.target_language_combo.pack(side=tk.LEFT, padx=(7, 12))
        self.target_language_combo.bind("<<ComboboxSelected>>", self._translation_language_changed)
        self.quick_button = ttk.Button(
            auto_row, text="Auto Translate", command=self.quick_auto, style="Secondary.TButton",
        )
        self.quick_button.pack(side=tk.LEFT, padx=(10, 4))
        self.online_button = ttk.Button(
            auto_row, text="Auto Translate + Shorten", command=self.online_auto,
            style="Secondary.TButton",
        )
        self.online_button.pack(side=tk.LEFT, padx=4)

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
        scroll.pack(side=tk.RIGHT, fill=tk.Y, padx=(0, 20), pady=(8, 0))
        self.text_tree.pack(fill=tk.BOTH, expand=True, padx=(20, 0), pady=(8, 0))
        self.text_tree.bind("<<TreeviewSelect>>", self._text_selected)
        self.text_tree.bind("<Motion>", self._text_tree_hover)
        self.text_tree.bind("<Button-1>", self._text_tree_clicked, add="+")
        self.text_tree.bind("<Button-3>", self._text_tree_clicked, add="+")
        self.japanese_text_menu = tk.Menu(
            self, tearoff=False, background=UI_CONTROL, foreground=UI_TEXT,
            activebackground=UI_ACTIVE, activeforeground="white", font=("Consolas", 9),
        )
        self.japanese_text_menu.add_command(
            label="Copy Japanese to Clipboard", command=self._copy_selected_japanese,
        )

        editor = ttk.LabelFrame(
            self.text_tab, text="Selected string", padding=10, style="Toolkit.TLabelframe",
        )
        editor.pack(fill=tk.X, padx=20, pady=(10, 18))
        self.original_var = tk.StringVar(value="Japanese text")
        ttk.Label(editor, textvariable=self.original_var, wraplength=1060).grid(row=0, column=0, columnspan=5,
                                                                               sticky=tk.W, pady=(0, 6))
        ttk.Label(editor, text="English:").grid(row=1, column=0, sticky=tk.W)
        self.translation_entry = tk.Text(
            editor, height=3, wrap=tk.WORD, undo=True, background=UI_CONTROL,
            foreground=UI_TEXT, insertbackground=UI_TEXT, selectbackground=UI_ACTIVE,
            relief=tk.FLAT, font=("Consolas", 10), padx=8, pady=6,
        )
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
        self._page_heading(
            self.image_tab, "Graphics Studio",
            "Preview, export, replace, and validate ROM artwork without losing palette behavior.",
            UI_BLUE,
        )
        pane = ttk.Panedwindow(self.image_tab, orient=tk.HORIZONTAL)
        pane.pack(fill=tk.BOTH, expand=True, padx=20, pady=(0, 18))
        left = ttk.Frame(pane, padding=10, style="Toolkit.TFrame")
        right = ttk.Frame(pane, padding=10, style="Toolkit.TFrame")
        pane.add(left, weight=1)
        pane.add(right, weight=3)

        ttk.Label(left, text="Filter assets:").pack(anchor=tk.W)
        self.image_filter = tk.StringVar()
        ttk.Entry(left, textvariable=self.image_filter).pack(fill=tk.X, pady=(3, 6))
        self.image_filter.trace_add("write", lambda *_: self.refresh_images())
        list_frame = ttk.Frame(left, style="Toolkit.TFrame")
        list_frame.pack(fill=tk.BOTH, expand=True)
        self.image_list = tk.Listbox(
            list_frame, exportselection=False, width=40, background=UI_CONTROL,
            foreground=UI_TEXT, selectbackground=UI_ACTIVE, selectforeground="white",
            relief=tk.FLAT, borderwidth=0, highlightthickness=1,
            highlightbackground=UI_BORDER, font=("Consolas", 9),
        )
        image_scroll = ttk.Scrollbar(list_frame, orient=tk.VERTICAL, command=self.image_list.yview)
        self.image_list.configure(yscrollcommand=image_scroll.set)
        self.image_list.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)
        image_scroll.pack(side=tk.RIGHT, fill=tk.Y)
        self.image_list.bind("<<ListboxSelect>>", self._image_selected)

        self.image_info = tk.StringVar(value="Select a CHBG asset")
        ttk.Label(right, textvariable=self.image_info).pack(anchor=tk.W)
        preview_frame = ttk.Frame(right, relief=tk.SUNKEN, borderwidth=1, style="Toolkit.TFrame")
        preview_frame.pack(fill=tk.BOTH, expand=True, pady=8)
        self.preview_canvas = tk.Canvas(preview_frame, background="#090d12", highlightthickness=0)
        self.preview_canvas.pack(fill=tk.BOTH, expand=True)
        self.preview_canvas.bind("<Configure>", lambda _event: self._draw_preview())

        buttons = ttk.Frame(right, style="Toolkit.TFrame")
        buttons.pack(fill=tk.X)
        ttk.Button(buttons, text="Export PNG", command=self.export_image).pack(side=tk.LEFT, padx=4)
        ttk.Button(buttons, text="Import PNG", command=self.import_image).pack(side=tk.LEFT, padx=4)
        ttk.Button(buttons, text="Revert Image", command=self.revert_image).pack(side=tk.LEFT, padx=4)
        ttk.Label(buttons, text=(
            "Source pixels are retained; the DS palette preview is validated against the "
            f"{100 + CHBG_SIZE_ALLOWANCE_PERCENT}% limit."
        )).pack(
            side=tk.RIGHT)

    def _build_compile_page(self) -> None:
        page = self.pages["compile"]
        self._page_heading(
            page, "Compile ROM", "Build a standard translated ROM and verify it before testing.",
            UI_GREEN,
        )
        action = tk.Frame(
            page, background=UI_PANEL, padx=18, pady=14,
            highlightthickness=1, highlightbackground=UI_BORDER,
        )
        action.pack(fill=tk.X, padx=20)
        tk.Label(
            action,
            text="This build uses the loaded text and graphics but does not include a direct-boot project.",
            background=UI_PANEL, foreground=UI_MUTED, font=("Consolas", 9),
        ).pack(side=tk.LEFT)
        self.compile_button = ttk.Button(
            action, text="Compile Standard ROM", command=self.compile, style="Primary.TButton",
        )
        self.compile_button.pack(side=tk.RIGHT)
        log_frame = tk.Frame(page, background=UI_BORDER, padx=1, pady=1)
        log_frame.pack(fill=tk.BOTH, expand=True, padx=20, pady=(12, 18))
        tk.Label(
            log_frame, text="Build Log", background=UI_PANEL, foreground=UI_BLUE,
            font=("Consolas", 10, "bold"), anchor=tk.W, padx=12, pady=8,
        ).pack(fill=tk.X)
        self.log = tk.Text(
            log_frame, wrap=tk.WORD, state=tk.DISABLED, background=UI_BG,
            foreground=UI_TEXT, insertbackground="white", relief=tk.FLAT,
            font=("Consolas", 9), padx=12, pady=10,
        )
        self.log.pack(fill=tk.BOTH, expand=True)

    def _build_direct_boot_page(self) -> None:
        page = self.pages["direct"]
        self._page_heading(
            page, "Direct-Boot ROM Builder",
            "Turn one created RPG Maker project into a ROM that launches the game automatically.",
            UI_BLUE,
        )
        content = tk.Frame(
            page, background=UI_PANEL, padx=22, pady=22,
            highlightthickness=1, highlightbackground=UI_BORDER,
        )
        content.pack(fill=tk.BOTH, expand=True, padx=20, pady=(0, 20))
        tk.Label(
            content, text="Build From a Save File", background=UI_PANEL, foreground=UI_GREEN,
            font=("Consolas", 11, "bold"),
        ).pack(anchor=tk.W)
        tk.Label(
            content,
            text=("Choose a DS+ .sav or DeSmuME .dsv file, select one valid created-game slot, then "
                  "choose an output ROM. The selected game is used only for this build. Your loaded "
                  ".rpgdsproj remains unchanged, and normal Compile ROM builds remain standard."),
            background=UI_PANEL, foreground=UI_MUTED, font=("Consolas", 9),
            justify=tk.LEFT, wraplength=900,
        ).pack(anchor=tk.W, pady=(10, 20))
        notes = tk.Frame(
            content, background=UI_CONTROL, padx=16, pady=14,
            highlightthickness=1, highlightbackground=UI_BORDER,
        )
        notes.pack(fill=tk.X, pady=(0, 22))
        tk.Label(
            notes,
            text=("DIRECT-BOOT BEHAVIOR\nSkips boot logos, the title menu, and project picker. On a blank "
                  "save, the embedded project is installed into slot 1 and launched through the game's "
                  "native play path."),
            background=UI_CONTROL, foreground=UI_TEXT, font=("Consolas", 9),
            justify=tk.LEFT, wraplength=860,
        ).pack(anchor=tk.W)
        self.embed_button = ttk.Button(
            content, text="Select Save and Build Direct-Boot ROM",
            command=self.embed_project_from_save, style="Primary.TButton",
        )
        self.embed_button.pack(anchor=tk.W)

    def _build_audio_page(self) -> None:
        page = self.pages["audio"]
        self._page_heading(
            page, "Music & Sound Effects",
            "The home for audio discovery, preview, extraction, and replacement.",
            UI_GREEN,
        )
        pane = ttk.Panedwindow(page, orient=tk.HORIZONTAL)
        pane.pack(fill=tk.BOTH, expand=True, padx=20, pady=(0, 18))
        left = ttk.Frame(pane, padding=10, style="Toolkit.TFrame")
        right = ttk.Frame(pane, padding=14, style="Toolkit.TFrame")
        pane.add(left, weight=1); pane.add(right, weight=3)
        ttk.Label(left, text="Filter tracks:").pack(anchor=tk.W)
        self.audio_filter = tk.StringVar()
        ttk.Entry(left, textvariable=self.audio_filter).pack(fill=tk.X, pady=(3, 6))
        self.audio_filter.trace_add("write", lambda *_: self.refresh_audio())
        self.audio_list = tk.Listbox(
            left, exportselection=False, width=34, background=UI_CONTROL,
            foreground=UI_TEXT, selectbackground=UI_ACTIVE, selectforeground="white",
            relief=tk.FLAT, borderwidth=0, highlightthickness=1,
            highlightbackground=UI_BORDER, font=("Consolas", 9),
        )
        audio_scroll = ttk.Scrollbar(left, orient=tk.VERTICAL, command=self.audio_list.yview)
        self.audio_list.configure(yscrollcommand=audio_scroll.set)
        self.audio_list.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)
        audio_scroll.pack(side=tk.RIGHT, fill=tk.Y)
        self.audio_list.bind("<<ListboxSelect>>", self._audio_selected)

        self.audio_title = tk.StringVar(value="Open a ROM to inspect its SDAT audio")
        ttk.Label(right, textvariable=self.audio_title, font=("Consolas", 12, "bold")).pack(anchor=tk.W)
        self.audio_info = tk.StringVar(value="")
        ttk.Label(right, textvariable=self.audio_info, foreground=UI_MUTED,
                  wraplength=720, justify=tk.LEFT).pack(anchor=tk.W, pady=(8, 18))
        controls = ttk.Frame(right, style="Toolkit.TFrame")
        controls.pack(fill=tk.X)
        ttk.Button(controls, text="Play MIDI Sequence", command=self.preview_audio,
                   style="Primary.TButton").pack(side=tk.LEFT, padx=(0, 8))
        ttk.Button(controls, text="Stop", command=stop_audio).pack(side=tk.LEFT, padx=4)
        ttk.Button(controls, text="Export MIDI", command=self.export_audio_midi).pack(side=tk.LEFT, padx=4)
        ttk.Button(controls, text="Import MIDI", command=self.import_audio_midi).pack(side=tk.LEFT, padx=4)
        ttk.Button(controls, text="Revert", command=self.revert_audio).pack(side=tk.LEFT, padx=4)
        tools = ttk.LabelFrame(right, text="Sound-bank tools", padding=14,
                               style="Toolkit.TLabelframe")
        tools.pack(fill=tk.X, pady=(24, 0))
        ttk.Label(
            tools,
            text=("Extracts every SSEQ/SSAR sequence, SBNK instrument bank, SWAR archive, "
                  "SWAV sample and decoded WAV. Extracted files stay outside the project and Git."),
            wraplength=700, justify=tk.LEFT,
        ).pack(anchor=tk.W, pady=(0, 10))
        ttk.Button(tools, text="Extract Complete Sound Library",
                   command=self.extract_audio_library).pack(anchor=tk.W)
        self.audio_note = tk.StringVar(value="")
        ttk.Label(right, textvariable=self.audio_note, foreground=UI_GREEN,
                  wraplength=720, justify=tk.LEFT).pack(anchor=tk.W, pady=(20, 0))

    def refresh_audio(self) -> None:
        if not hasattr(self, "audio_list"):
            return
        query = self.audio_filter.get().strip().lower()
        self.filtered_audio_assets = [
            asset for asset in self.audio_assets
            if not query or query in asset.label.lower()
        ]
        self.audio_list.delete(0, tk.END)
        for asset in self.filtered_audio_assets:
            changed = " *" if asset.key in self.audio_replacements else ""
            self.audio_list.insert(tk.END, asset.label + changed)

    def _audio_selected(self, _event=None) -> None:
        selection = self.audio_list.curselection()
        if not selection:
            return
        self.current_audio = self.filtered_audio_assets[selection[0]]
        asset = self.current_audio
        bank = self.sdat.banks[asset.bank_id][1]
        wave_ids = bank.waveArchiveIDs or []
        sample_count = sum(len(self.sdat.waveArchives[index][1].waves)
                           for index in wave_ids if index is not None)
        self.audio_title.set(f"{asset.name}  ({asset.kind.upper()})")
        self.audio_info.set(
            f"Sequence ID {asset.index}  |  Bank {asset.bank_id}  |  "
            f"{len(bank.instruments)} instrument slots  |  {sample_count} ADPCM samples  |  "
            f"Player {asset.player_id}  |  Archive volume {asset.volume}"
        )
        self.audio_note.set(
            "MIDI replacement loaded; previews and compiled ROMs use it."
            if asset.key in self.audio_replacements else
            "Preview uses the original sequence and the ROM's native instrument samples."
        )

    def _selected_audio_sequence(self):
        if not self.current_audio or not self.sdat:
            raise ValueError("Select an audio track first")
        original = sequence_for_asset(self.sdat, self.current_audio)
        replacement = self.audio_replacements.get(self.current_audio.key)
        if replacement:
            return ndspy.soundSequence.SSEQ(
                replacement, getattr(original, "unk02", 0), original.bankID,
                original.volume, original.channelPressure,
                original.polyphonicPressure, original.playerID,
            )
        return original

    def preview_audio(self) -> None:
        if not self.current_audio:
            messagebox.showinfo(APP_NAME, "Select a music or sound-effect entry first.")
            return
        asset = self.current_audio
        def task():
            sequence = self._selected_audio_sequence()
            duration = 8.0 if asset.kind in {"se", "me"} else 30.0
            return render_sequence_ncsf_pcm(self.sdat, asset, sequence, duration)
        def done(result):
            pcm, sample_rate = result
            try:
                play_pcm_bytes(pcm, sample_rate)
            except Exception as exc:
                messagebox.showerror(APP_NAME, f"Audio output failed:\n{exc}")
                return
            self.status_var.set(f"Playing {asset.name}")
            self.audio_note.set("Playing with the in_ncsf / FeOS DS audio engine; no WAV file created.")
        self.status_var.set(f"Rendering {asset.name} with the accurate DS audio engine...")
        self._run_worker(task, done)

    def export_audio_midi(self) -> None:
        if not self.current_audio:
            messagebox.showinfo(APP_NAME, "Select an audio entry first.")
            return
        path = filedialog.asksaveasfilename(
            title="Export MIDI", defaultextension=".mid",
            initialfile=safe_filename(self.current_audio.name) + ".mid",
            filetypes=(("MIDI file", "*.mid"),),
        )
        if not path:
            return
        try:
            sequence_to_midi(self._selected_audio_sequence()).save(path)
            self.status_var.set(f"Exported MIDI: {Path(path).name}")
        except Exception as exc:
            messagebox.showerror(APP_NAME, f"MIDI export failed:\n{exc}")

    def _choose_midi_instruments(self, midi, bank) -> list[int] | None:
        names = midi_track_names(midi)
        dialog = tk.Toplevel(self)
        dialog.title("Assign DS instruments")
        dialog.configure(background=UI_PANEL)
        dialog.transient(self); dialog.grab_set()
        tk.Label(
            dialog, text="Assign each MIDI track to an instrument in the selected DS sound bank.",
            background=UI_PANEL, foreground=UI_TEXT, font=("Consolas", 10, "bold"),
            padx=16, pady=14,
        ).pack(anchor=tk.W)
        body = tk.Frame(dialog, background=UI_PANEL, padx=16)
        body.pack(fill=tk.BOTH, expand=True)
        instrument_labels = []
        for index, instrument in enumerate(bank.instruments):
            if instrument is not None:
                instrument_labels.append(f"{index:03d}  {type(instrument).__name__}")
        if not instrument_labels:
            dialog.destroy()
            raise ValueError("The selected track has no usable instruments")
        variables = []
        for index, name in enumerate(names[:16]):
            row = tk.Frame(body, background=UI_PANEL)
            row.pack(fill=tk.X, pady=3)
            tk.Label(row, text=name, width=30, anchor=tk.W, background=UI_PANEL,
                     foreground=UI_TEXT).pack(side=tk.LEFT)
            default = instrument_labels[min(index, len(instrument_labels) - 1)]
            variable = tk.StringVar(value=default)
            ttk.Combobox(row, textvariable=variable, values=instrument_labels,
                         state="readonly", width=34).pack(side=tk.LEFT)
            variables.append(variable)
        result = []
        def accept():
            result.extend(int(variable.get().split()[0]) for variable in variables)
            dialog.destroy()
        buttons = tk.Frame(dialog, background=UI_PANEL, padx=16, pady=14)
        buttons.pack(fill=tk.X)
        ttk.Button(buttons, text="Import MIDI", command=accept,
                   style="Primary.TButton").pack(side=tk.RIGHT)
        ttk.Button(buttons, text="Cancel", command=dialog.destroy).pack(side=tk.RIGHT, padx=8)
        dialog.protocol("WM_DELETE_WINDOW", dialog.destroy)
        self.wait_window(dialog)
        return result or None

    def import_audio_midi(self) -> None:
        if not self.current_audio:
            messagebox.showinfo(APP_NAME, "Select the music slot that the MIDI should replace.")
            return
        if self.current_audio.kind == "se":
            messagebox.showinfo(
                APP_NAME, "MIDI replacement currently targets BGM, BGS and ME slots. "
                "Sound effects can be previewed and extracted.",
            )
            return
        path = filedialog.askopenfilename(title="Import MIDI",
                                          filetypes=(("MIDI file", "*.mid *.midi"),))
        if not path:
            return
        try:
            midi = mido.MidiFile(path)
            bank = self.sdat.banks[self.current_audio.bank_id][1]
            assignments = self._choose_midi_instruments(midi, bank)
            if assignments is None:
                return
            original = sequence_for_asset(self.sdat, self.current_audio)
            replacement = midi_to_sequence(midi, assignments, original)
            raw = bytes(replacement.save()[0])
            # Reparse now so malformed event graphs are rejected before saving/building.
            ndspy.soundSequence.SSEQ(raw, original.unk02, original.bankID,
                                     original.volume, original.channelPressure,
                                     original.polyphonicPressure, original.playerID).parse()
            self.audio_replacements[self.current_audio.key] = raw
            self.refresh_audio(); self._audio_selected()
            self.status_var.set(f"Imported MIDI into {self.current_audio.name}")
        except Exception as exc:
            messagebox.showerror(APP_NAME, f"MIDI import failed:\n{exc}")

    def revert_audio(self) -> None:
        if not self.current_audio:
            return
        self.audio_replacements.pop(self.current_audio.key, None)
        self.refresh_audio(); self._audio_selected()
        self.status_var.set(f"Reverted {self.current_audio.name}")

    def extract_audio_library(self) -> None:
        if not self.sdat:
            messagebox.showinfo(APP_NAME, "Open a ROM first.")
            return
        selected = filedialog.askdirectory(title="Choose sound-library output folder")
        if not selected:
            return
        def task(): return export_audio_workspace(self.sdat, Path(selected))
        def done(manifest):
            self.status_var.set("Complete sound library extracted")
            messagebox.showinfo(
                APP_NAME, f"Extracted {len(manifest['assets'])} sequences, "
                f"{len(manifest['banks'])} banks and {len(manifest['wave_archives'])} "
                f"wave archives to:\n{selected}",
            )
        self.status_var.set("Extracting all DS sound banks and samples...")
        self._run_worker(task, done)

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
                  saved_embedded_project: EmbeddedProject | None = None,
                  saved_audio: dict[str, bytes] | None = None) -> None:
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
            self.audio_replacements = dict(saved_audio or {})
            self.sdat = ndspy.soundArchive.SDAT(bytes(self.rom.getFileByName(SDAT_ROM_PATH)))
            self.audio_assets = list_audio_assets(self.sdat)
            if saved_rows:
                for entry in self.entries:
                    row = saved_rows.get(entry.key)
                    if row:
                        entry.translation = row.get("translation", "")
                        entry.auto = row.get("auto", "False").lower() == "true"
            self.quick_auto(silent=True)
            self.refresh_texts()
            self.refresh_images()
            self.refresh_audio()
            self.status_var.set(f"Loaded {self.profile.title}: {len(self.entries)} strings, {len(self.images)} images")
            code = bytes(self.rom.idCode).decode("ascii", errors="replace")
            project_name = self.project_path.name if self.project_path else "None"
            self.session_var.set(
                f"Original ROM loaded  |  Toolkit project: {project_name}"
            )
            if hasattr(self, "project_save_var"):
                self.project_save_var.set("")
            self.title(f"{APP_NAME} - {self.profile.title} [{code}]")
            self.append_log(f"Opened {path}\nSHA-256: {digest}\nFound {len(self.entries)} text slots and "
                            f"{len(self.images)} CHBG images.")
            if saved_embedded_project is not None:
                self.append_log(
                    "This older project archive contains an embedded game, but it was not "
                    "attached to the editing session. Use Build Direct-Boot ROM from Save "
                    "for a one-time standalone build."
                )
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
            audio = load_project_audio(path)
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
            saved_audio=audio,
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
            audio_replacements = getattr(self, "audio_replacements", {})
            if audio_replacements:
                save_project(
                    path, self.source_rom, self.entries, self.image_pngs, None,
                    audio_replacements,
                )
            else:
                save_project(path, self.source_rom, self.entries, self.image_pngs, None)
            self.project_path = path
            self._remember_session("project", self.source_rom, path)
            if hasattr(self, "session_var"):
                self.session_var.set(
                    f"Original ROM loaded  |  Toolkit project: {path.name}"
                )
            if hasattr(self, "project_save_var"):
                self.project_save_var.set("Project saved")
            self.status_var.set(f"Project saved: {path.name}")
            self.append_log(f"Saved project to {path}")
        except Exception as exc:
            messagebox.showerror(APP_NAME, str(exc))

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
                "The selected slot will be used for this ROM build only. The currently "
                "loaded .rpgdsproj file and normal Compile ROM output will not be changed."
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

        buttons = ttk.Frame(dialog, padding=12)
        buttons.pack(fill=tk.X)
        ttk.Button(buttons, text="Cancel", command=dialog.destroy).pack(side=tk.RIGHT, padx=(8, 0))
        embed_button = ttk.Button(buttons, text="Compile Selected Project", command=accept)
        embed_button.pack(side=tk.RIGHT)
        tree.bind("<Double-1>", lambda _event: accept())
        dialog.protocol("WM_DELETE_WINDOW", dialog.destroy)
        self.wait_window(dialog)

        project = result.get("project")
        if isinstance(project, EmbeddedProject):
            output = filedialog.asksaveasfilename(
                parent=self,
                title="Compile direct-boot ROM",
                defaultextension=".nds",
                initialfile="RPG Tsukuru DS+ Direct Boot.nds",
                filetypes=(("Nintendo DS ROM", "*.nds"),),
            )
            if not output:
                return
            self.append_log(
                f"One-time direct-boot build selected {save_path.name} slot "
                f"{project.source_slot}.\nProject slot SHA-256: {project.sha256}\n"
                "The loaded .rpgdsproj was not modified."
            )
            self._start_compile(Path(output), project)

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

    def _text_tree_hover(self, event) -> None:
        is_japanese = bool(
            self.text_tree.identify_row(event.y)
            and self.text_tree.identify_column(event.x) == "#2"
        )
        self.text_tree.configure(cursor="hand2" if is_japanese else "")

    def _text_tree_clicked(self, event):
        row = self.text_tree.identify_row(event.y)
        column = self.text_tree.identify_column(event.x)
        if not row or column != "#2":
            return None
        self.text_tree.selection_set(row)
        self.text_tree.focus(row)
        self._text_selected()
        try:
            self.japanese_text_menu.tk_popup(event.x_root, event.y_root)
        finally:
            self.japanese_text_menu.grab_release()
        return "break"

    def _copy_selected_japanese(self) -> None:
        entry = self._selected_entry()
        if not entry:
            return
        self.clipboard_clear()
        self.clipboard_append(entry.original)
        self.update_idletasks()
        self.status_var.set("Copied Japanese text to the clipboard.")

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
            raw_value = self._get_translation_text()
            used = 0 if raw_value and raw_value.isspace() else len(raw_value.encode("cp932"))
            if raw_value and raw_value.isspace():
                state = "BLANK IN ROM"
            elif used <= entry.max_bytes:
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
        raw_value = self._get_translation_text()
        # A whitespace-only edit is an explicit request for an empty ROM
        # string. Persist one space as the marker; the compiler emits only NUL
        # bytes. An actually empty editor still means "use the Japanese text".
        value = " " if raw_value and raw_value.isspace() else raw_value.strip()
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

    def _target_language_code(self) -> str:
        return TRANSLATION_LANGUAGES.get(self.target_language_var.get(), "en")

    def _translation_language_changed(self, _event=None) -> None:
        code = self._target_language_code()
        self.session_settings["translation_language"] = code
        self._persist_session_settings()
        self.status_var.set(f"Google Translate target set to {self.target_language_var.get()}.")

    def quick_auto(self, silent: bool = False) -> None:
        if not self.entries:
            if not silent:
                messagebox.showinfo(APP_NAME, "Open a ROM first.")
            return
        if silent:
            completed, _ = auto_translate_entries(
                self.entries, online=False, target_language=self._target_language_code(),
            )
            self.refresh_texts()
            return
        self._start_auto_translation(require_confirmation=False)

    def online_auto(self) -> None:
        self._start_auto_translation(require_confirmation=True)

    def _start_auto_translation(self, require_confirmation: bool) -> None:
        if not self.entries:
            messagebox.showinfo(APP_NAME, "Open a ROM first.")
            return
        pending = [entry for entry in self.entries if not entry.translation]
        if not pending:
            messagebox.showinfo(APP_NAME, "Every extracted string already has a translation.")
            return
        language_name = self.target_language_var.get()
        target_language = self._target_language_code()
        if require_confirmation:
            if not messagebox.askyesno(
                APP_NAME,
                f"Translate and aggressively shorten {len(pending)} pending strings to "
                f"{language_name}?\n\nThe tool will reduce sentences to short UI labels and "
                "RPG-style codes where space is extremely tight. Required tokens and negative "
                "meaning are preserved. Review auto translations before release.",
            ):
                return

        def progress(current, total):
            self.worker_queue.put(("progress", current, total, f"Auto translating {current}/{total}..."))

        def task():
            return auto_translate_entries(
                pending, progress=progress, online=True, target_language=target_language,
            )

        def done(result):
            completed, skipped = result
            self.refresh_texts()
            self.status_var.set(
                f"{language_name} auto translation complete: {completed} added, "
                f"{skipped} need manual editing."
            )
            self.append_log(
                f"Google Translate ({target_language}) added {completed} fitted strings; "
                f"{skipped} did not fit."
            )
        self.status_var.set(f"Starting Google Translate to {language_name}...")
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
                    replacement = sanitize_import_image(source_image)
                original = bytes(self.rom.files[asset.file_id])
                if asset.kind == "BMBG":
                    encoded = encode_bmbg(
                        replacement, original, asset.compressed, self._asset_palette(asset),
                    )
                    image = decode_bmbg(
                        encoded, asset.compressed, self._asset_palette(asset),
                    )
                else:
                    prepared = prepare_chbg_replacement(
                        replacement.convert("RGBA"), original, asset.compressed,
                        asset.name.lower() == "wifi/castle-logo.bin",
                    )
                    image = decode_chbg(prepared.data, asset.compressed)
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
            clean_source_png = sanitize_png_bytes(png)
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
                self.image_pngs[self.current_image.name] = clean_source_png
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
            # Keep the sanitized source pixels in the project. The encoded
            # preview is palette-normalized for the DS, but retaining the
            # source prevents repeated imports from permanently discarding
            # shades and lets future encoder improvements rebuild it cleanly.
            self.image_pngs[self.current_image.name] = clean_source_png
            self.preview_image = normalized
            self.refresh_images()
            self._draw_preview()
            palette_note = (
                f"; {prepared.palette_adjusted_pixels:,} pixels matched to the original palette"
                if prepared.palette_adjusted_pixels else ""
            )
            cleanup_note = (
                "; source metadata removed while retaining the source pixels"
                if metadata_removed
                else "; metadata-free source pixels retained in the project"
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
        self._start_compile(Path(output), None)

    def _start_compile(self, output_path: Path,
                       embedded_project: EmbeddedProject | None) -> None:
        """Compile either a normal ROM or a one-time direct-boot ROM.

        The embedded project is an argument owned only by this build job. It is
        never attached to the editing session or written to the .rpgdsproj.
        """
        if not self.source_rom:
            messagebox.showinfo(APP_NAME, "Open a ROM first.")
            return
        invalid = [entry for entry in self.entries if entry.translation and not entry.valid]
        if invalid:
            messagebox.showerror(APP_NAME, f"Fix {len(invalid)} invalid translations before compiling.")
            return

        def progress(current, total, status):
            self.worker_queue.put(("progress", current, total, status))

        def task():
            progress(1, 3, "Applying text and image replacements...")
            result = compile_rom(
                self.source_rom, output_path, self.entries, self.image_pngs,
                embedded_project, getattr(self, "audio_replacements", {}),
            )
            progress(2, 3, "Verifying rebuilt ROM...")
            rebuilt = ndspy.rom.NintendoDSRom.fromFile(output_path)
            rebuilt.loadArm9Overlays()
            if bytes(rebuilt.idCode) != self.profile.game_code:
                raise ValueError("Rebuilt ROM verification failed")
            if embedded_project:
                embedded_id = rebuilt.filenames.idOf(EMBEDDED_PROJECT_ROM_PATH)
                if embedded_id is None:
                    raise ValueError("Rebuilt ROM is missing the embedded project asset")
                if bytes(rebuilt.files[embedded_id]) != embedded_project.data:
                    raise ValueError("Rebuilt ROM embedded-project verification failed")
            progress(3, 3, "Compilation complete.")
            return result

        def done(result):
            text_count, image_count = result
            self.status_var.set(f"Compiled {output_path.name}")
            self.append_log(f"Compiled {output_path}\nApplied {text_count} text translations and "
                            f"{image_count} image replacements. Rebuilt ROM parsed successfully."
                            + (
                                f"\nEmbedded created-game slot {embedded_project.source_slot} "
                                f"as {EMBEDDED_PROJECT_ROM_PATH}; cold-boot installer enabled."
                                if embedded_project else ""
                            ))
            self.show_page("compile")
            mode = "Direct-boot ROM" if embedded_project else "ROM"
            messagebox.showinfo(
                APP_NAME,
                f"{mode} compiled successfully.\n\n{output_path}\n\n"
                f"Text changes: {text_count}\nImage changes: {image_count}"
                + (
                    "\n\nThe loaded .rpgdsproj was not modified."
                    if embedded_project else ""
                ),
            )
        self.status_var.set(
            "Compiling one-time direct-boot ROM..."
            if embedded_project else "Compiling ROM..."
        )
        self._run_worker(task, done)


def main() -> None:
    app = TranslatorApp()
    app.mainloop()


if __name__ == "__main__":
    main()
