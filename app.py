from __future__ import annotations

import queue
import json
import threading
import time
import tkinter as tk
import subprocess
import logging
from logging.handlers import RotatingFileHandler
import platform
import tempfile
import os
import re
from pathlib import Path
from tkinter import filedialog, messagebox, ttk

from kindle_converter import (ConversionCancelled, ConversionOptions, available_ocr_languages,
                              convert_pdf_to_epub, ensure_ocr_language, fetch_openai_models,
                              recommend_openai_models, validate_epub)

LANGUAGE_NAMES = {
    "dan": "Dansk", "eng": "Engelsk", "deu": "Tysk", "fra": "Fransk",
    "spa": "Spansk", "ita": "Italiensk", "nld": "Nederlandsk", "nor": "Norsk",
    "swe": "Svensk", "fin": "Finsk", "isl": "Islandsk", "por": "Portugisisk",
    "pol": "Polsk", "ukr": "Ukrainsk", "ces": "Tjekkisk",
}
SUPPORTED_LANGUAGES = ("dan", "eng", "deu", "fra", "spa", "nor", "swe")
KEYCHAIN_SERVICE = "dk.local.pdftokindle.openai"
KEYCHAIN_ACCOUNT = "PDF til Kindle"
MODEL_CACHE_PATH = Path.home() / "Library" / "Application Support" / "PDF til Kindle" / "model-catalog.json"


def load_model_cache():
    try:
        payload = json.loads(MODEL_CACHE_PATH.read_text(encoding="utf-8"))
        if payload.get("version") != 1:
            return {}
        models = payload.get("models", [])
        metadata = payload.get("metadata", {})
        recommendations = payload.get("recommendations", [])
        if not isinstance(models, list) or not all(isinstance(item, str) for item in models):
            return {}
        if not isinstance(metadata, dict) or not isinstance(recommendations, list):
            return {}
        return payload
    except (OSError, ValueError, TypeError, AttributeError):
        return {}


def save_model_cache(models, metadata, recommendations, selected_model, illustration_model=""):
    MODEL_CACHE_PATH.parent.mkdir(parents=True, exist_ok=True)
    temporary = MODEL_CACHE_PATH.with_suffix(".tmp")
    temporary.write_text(json.dumps({
        "version": 1,
        "updated_at": time.time(),
        "models": models,
        "metadata": metadata,
        "recommendations": recommendations,
        "selected_model": selected_model,
        "illustration_model": illustration_model,
    }, ensure_ascii=False, indent=2), encoding="utf-8")
    temporary.replace(MODEL_CACHE_PATH)


def setup_logging():
    candidates = [Path.home() / "Library" / "Logs" / "PDF til Kindle" / "app.log",
                  Path(tempfile.gettempdir()) / "pdf-til-kindle-app.log"]
    for path in candidates:
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            handler = RotatingFileHandler(path, maxBytes=2_000_000, backupCount=3, encoding="utf-8")
            handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(name)s: %(message)s"))
            root = logging.getLogger()
            root.setLevel(logging.INFO)
            root.addHandler(handler)
            return path
        except OSError:
            continue
    return candidates[-1]


LOG_PATH = setup_logging()
logger = logging.getLogger("pdf_to_kindle.app")


def load_api_key():
    result = subprocess.run(["/usr/bin/security", "find-generic-password", "-s", KEYCHAIN_SERVICE,
                             "-a", KEYCHAIN_ACCOUNT, "-w"], text=True,
                            stdout=subprocess.PIPE, stderr=subprocess.DEVNULL)
    return result.stdout.strip() if result.returncode == 0 else ""


def save_api_key(value):
    result = subprocess.run(["/usr/bin/security", "add-generic-password", "-U", "-s", KEYCHAIN_SERVICE,
                             "-a", KEYCHAIN_ACCOUNT, "-w", value],
                            stdout=subprocess.DEVNULL, stderr=subprocess.PIPE, text=True)
    if result.returncode:
        raise RuntimeError(result.stderr.strip() or "Nøglen kunne ikke gemmes i macOS-nøglering.")


def forget_api_key():
    subprocess.run(["/usr/bin/security", "delete-generic-password", "-s", KEYCHAIN_SERVICE,
                    "-a", KEYCHAIN_ACCOUNT], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)


class App(tk.Tk):
    def __init__(self):
        super().__init__()
        self.title("PDF til Kindle")
        icon_candidates = [Path(__file__).resolve().parent / "AppIcon.png",
                           Path(__file__).resolve().parent / "assets" / "app-icon.png"]
        for icon_path in icon_candidates:
            if icon_path.exists():
                try:
                    self.app_icon = tk.PhotoImage(file=str(icon_path))
                    self.iconphoto(True, self.app_icon)
                except tk.TclError:
                    pass
                break
        self.geometry("720x560")
        self.minsize(640, 440)
        self.events = queue.Queue()
        self.cancel_event = None
        self.started_at = None
        self.cloud_wait_started = None
        self.cloud_wait_prefix = ""
        self.require_ai_confirmation = True
        self.run_warning = None
        self.closing_requested = False
        self.caffeinate_process = None
        self.pdf = tk.StringVar()
        self.title_value = tk.StringVar()
        self.author = tk.StringVar()
        installed_codes = set(available_ocr_languages())
        codes = list(SUPPORTED_LANGUAGES)
        self.language_codes = {LANGUAGE_NAMES.get(code, code.upper()): code for code in codes}
        default_code = "dan" if "dan" in installed_codes else "eng"
        self.language = tk.StringVar(value=next(name for name, code in self.language_codes.items() if code == default_code))
        self.keep_visuals = tk.BooleanVar(value=True)
        self.ai_enabled = tk.BooleanVar(value=False)
        self.ai_cleanup = tk.BooleanVar(value=True)
        self.ai_illustrations = tk.BooleanVar(value=False)
        saved_key = load_api_key()
        cached_models = load_model_cache()
        self.api_key = tk.StringVar(value=saved_key)
        self.save_key = tk.BooleanVar(value=True)
        self.has_saved_key = bool(saved_key)
        self.ai_model = tk.StringVar(value=cached_models.get("selected_model", ""))
        self.available_models = cached_models.get("models", [])
        cached_illustration_model = cached_models.get("illustration_model", "gpt-5.6-luna")
        if self.available_models and cached_illustration_model not in self.available_models:
            cached_illustration_model = (
                "gpt-5.6-luna" if "gpt-5.6-luna" in self.available_models
                else (self.ai_model.get() if self.ai_model.get() in self.available_models else self.available_models[0]))
        self.illustration_model = tk.StringVar(value=cached_illustration_model)
        self.model_metadata = cached_models.get("metadata", {})
        self.model_recommendations = cached_models.get("recommendations", [])
        self.models_loaded = bool(self.available_models)
        self.models_loading = False
        self.ai_workflow = tk.StringVar(value="confirm")
        self.model_help = tk.StringVar(value="AI er slået fra. Dokumentet behandles kun lokalt.")
        self.phase_text = tk.StringVar(value="Klar")
        self.eta_text = tk.StringVar(value="")
        self.advanced_enabled = tk.BooleanVar(value=False)
        self.settings_controls = []
        self.dark_mode = self._system_dark_mode()
        self.colors = self._theme_colors(self.dark_mode)
        self.style = ttk.Style(self)
        self._apply_theme()
        self._build(list(self.language_codes))
        self.protocol("WM_DELETE_WINDOW", self._on_close)
        self.after_idle(self._fit_window)
        self.after(100, self._poll)
        self.after(3000, self._watch_appearance)

    @staticmethod
    def _system_dark_mode():
        """Read the effective macOS appearance without requiring extra packages."""
        if platform.system() != "Darwin":
            return False
        try:
            result = subprocess.run(
                ["/usr/bin/defaults", "read", "-g", "AppleInterfaceStyle"],
                stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, text=True, timeout=1,
            )
            return result.returncode == 0 and result.stdout.strip().casefold() == "dark"
        except (OSError, subprocess.SubprocessError):
            return False

    @staticmethod
    def _theme_colors(dark):
        if dark:
            return {
                "window": "#1c1c1e", "surface": "#252527", "field": "#303033",
                "text": "#f2f2f7", "muted": "#b0b0b6", "border": "#48484a",
                "accent": "#64b5f6", "selection": "#0a84ff", "danger": "#ff6961",
                "disabled": "#77777d",
            }
        return {
            "window": "#f2f2f2", "surface": "#f7f7f7", "field": "#ffffff",
            "text": "#242426", "muted": "#5c5c61", "border": "#c8c8cc",
            "accent": "#39779a", "selection": "#0a84ff", "danger": "#d70015",
            "disabled": "#8e8e93",
        }

    def _apply_theme(self):
        colors = self.colors
        self.configure(background=colors["window"])
        self.option_add("*selectBackground", colors["selection"])
        self.option_add("*selectForeground", "#ffffff")
        self.option_add("*TCombobox*Listbox.background", colors["field"])
        self.option_add("*TCombobox*Listbox.foreground", colors["text"])
        self.option_add("*TCombobox*Listbox.selectBackground", colors["selection"])
        self.option_add("*TCombobox*Listbox.selectForeground", "#ffffff")
        style = self.style
        style.configure(".", background=colors["window"], foreground=colors["text"])
        style.configure("TFrame", background=colors["window"])
        style.configure("TLabel", background=colors["window"], foreground=colors["text"])
        style.configure("TLabelframe", background=colors["window"], foreground=colors["text"],
                        bordercolor=colors["border"])
        style.configure("TLabelframe.Label", background=colors["window"], foreground=colors["text"])
        style.configure("TCheckbutton", background=colors["window"], foreground=colors["text"])
        style.configure("TRadiobutton", background=colors["window"], foreground=colors["text"])
        for widget_style in ("TCheckbutton", "TRadiobutton"):
            style.map(widget_style,
                      background=[("active", colors["window"])],
                      foreground=[("disabled", colors["disabled"]), ("active", colors["text"])])
        style.configure("TEntry", fieldbackground=colors["field"], foreground=colors["text"],
                        insertcolor=colors["text"], bordercolor=colors["border"])
        style.map("TEntry", fieldbackground=[("disabled", colors["surface"]),
                                             ("readonly", colors["field"])],
                  foreground=[("disabled", colors["disabled"]), ("readonly", colors["text"])])
        style.configure("TCombobox", fieldbackground=colors["field"], background=colors["field"],
                        foreground=colors["text"], arrowcolor=colors["text"])
        style.map("TCombobox", fieldbackground=[("readonly", colors["field"]),
                                                ("disabled", colors["surface"])],
                  foreground=[("readonly", colors["text"]), ("disabled", colors["disabled"])],
                  selectbackground=[("readonly", colors["field"])],
                  selectforeground=[("readonly", colors["text"])])
        style.configure("TButton", foreground=colors["text"])
        style.map("TButton", foreground=[("disabled", colors["disabled"]), ("active", colors["text"])])
        style.configure("Title.TLabel", font=("Helvetica", 21, "bold"), foreground=colors["text"])
        style.configure("Hint.TLabel", foreground=colors["muted"])
        style.configure("Phase.TLabel", font=("Helvetica", 12, "bold"), foreground=colors["text"])
        style.configure("StatusText.TLabel", font=("Helvetica", 11), foreground=colors["text"])
        style.configure("Eta.TLabel", font=("Helvetica", 13, "bold"), foreground=colors["text"])
        style.configure("Status.TLabelframe", relief="solid", borderwidth=1,
                        bordercolor=colors["border"])
        style.configure("Developer.TLabel", foreground=colors["accent"],
                        font=("Helvetica", 10, "underline"))
        style.configure("Horizontal.TProgressbar", troughcolor=colors["surface"],
                        background=colors["selection"], bordercolor=colors["border"])
        if hasattr(self, "canvas"):
            self.canvas.configure(background=colors["window"])
        if hasattr(self, "forget_key_button"):
            self.forget_key_button.configure(
                foreground=colors["danger"], activeforeground=colors["danger"],
                background=colors["window"], activebackground=colors["surface"],
            )
        for child in self.winfo_children():
            if isinstance(child, tk.Toplevel):
                child.configure(background=colors["window"])

    def _watch_appearance(self):
        dark_mode = self._system_dark_mode()
        if dark_mode != self.dark_mode:
            self.dark_mode = dark_mode
            self.colors = self._theme_colors(dark_mode)
            self._apply_theme()
        self.after(3000, self._watch_appearance)

    def _build(self, languages):
        style = self.style
        shell = ttk.Frame(self)
        shell.pack(fill="both", expand=True)
        self.canvas = tk.Canvas(shell, highlightthickness=0, borderwidth=0,
                                background=self.colors["window"])
        self.scrollbar = ttk.Scrollbar(shell, orient="vertical", command=self.canvas.yview)
        self.canvas.configure(yscrollcommand=self.scrollbar.set)
        self.scrollbar.pack(side="right", fill="y")
        self.canvas.pack(side="left", fill="both", expand=True)
        outer = ttk.Frame(self.canvas, padding=(18, 12))
        self.content_frame = outer
        self.canvas_window = self.canvas.create_window((0, 0), window=outer, anchor="nw")
        outer.bind("<Configure>", self._content_resized)
        self.canvas.bind("<Configure>", self._canvas_resized)
        self.bind_all("<MouseWheel>", self._mousewheel)
        header = ttk.Frame(outer)
        header.pack(fill="x")
        ttk.Label(header, text="PDF til Kindle", style="Title.TLabel").pack(side="left")
        self.developer_link = ttk.Label(header, text="Developer", style="Developer.TLabel", cursor="pointinghand")
        self.developer_link.pack(side="right", anchor="n", pady=(4, 0))
        self.developer_link.bind("<Button-1>", self._toggle_advanced)
        ttk.Label(outer, text="OCR, læsevenlig tekst og bevarede illustrationer i én EPUB.",
                  style="Hint.TLabel").pack(anchor="w", pady=(1, 8))
        form = ttk.Frame(outer)
        form.pack(fill="x")
        self._row(form, 0, "PDF-fil", self.pdf, "Vælg…", self._choose)
        self._row(form, 1, "Bogtitel", self.title_value)
        self._row(form, 2, "Forfatter", self.author)
        ttk.Label(form, text="OCR-sprog").grid(row=3, column=0, sticky="w", pady=5)
        self.language_picker = ttk.Combobox(form, textvariable=self.language, values=languages, state="readonly", width=18)
        self.language_picker.grid(row=3, column=1, sticky="w", pady=5)
        self.language_picker.bind("<<ComboboxSelected>>", self._language_selected)
        self.visuals_picker = ttk.Checkbutton(form, variable=self.keep_visuals, command=self._toggle_ai,
                                               text="Find og indsæt illustrationer automatisk")
        self.visuals_picker.grid(row=4, column=1, sticky="w", pady=5)
        self.ai_picker = ttk.Checkbutton(form, variable=self.ai_enabled, command=self._toggle_ai,
                                         text="Brug OpenAI API til at forbedre resultatet")
        self.ai_picker.grid(row=5, column=1, sticky="w", pady=(8, 4))

        self.ai_panel = ttk.LabelFrame(form, text="AI-indstillinger", padding=(10, 6))
        self.ai_panel.grid(row=6, column=0, columnspan=3, sticky="ew", pady=(1, 3))
        self.ai_panel.columnconfigure(1, weight=1)
        ttk.Label(self.ai_panel,
                  text="Designet til OpenAI API · kræver API-nøgle · afregnes separat fra ChatGPT",
                  style="Hint.TLabel").grid(row=0, column=0, columnspan=2, sticky="w", pady=(0, 3))
        ttk.Label(self.ai_panel, text="Behandl").grid(row=1, column=0, sticky="w", padx=(0, 12), pady=2)
        choices = ttk.Frame(self.ai_panel)
        choices.grid(row=1, column=1, sticky="w")
        self.ai_text_picker = ttk.Checkbutton(
            choices, variable=self.ai_cleanup, text="Tekst – ret OCR-fejl", command=self._toggle_ai)
        self.ai_text_picker.pack(side="left")
        self.ai_visuals_picker = ttk.Checkbutton(
            choices, variable=self.ai_illustrations,
            text="Billeder – find, beskær og placer illustrationer", command=self._toggle_ai,
        )
        self.ai_visuals_picker.pack(side="left", padx=(14, 0))

        self.model_label = ttk.Label(self.ai_panel, text="Model")
        self.model_label.grid(row=2, column=0, sticky="w", padx=(0, 12), pady=3)
        model_controls = ttk.Frame(self.ai_panel)
        model_controls.grid(row=2, column=1, sticky="ew", pady=3)
        self.model_picker = ttk.Combobox(model_controls, textvariable=self.ai_model,
                                         values=tuple(self.available_models), width=17, state="readonly")
        self.model_picker.pack(side="left")
        self.model_picker.bind("<<ComboboxSelected>>", self._model_selected)
        self.model_picker.bind("<FocusOut>", self._model_selected)
        self.refresh_models_button = ttk.Button(
            model_controls, text="Opdatér modeller" if self.models_loaded else "Hent modeller",
            command=self._refresh_models,
        )
        self.refresh_models_button.pack(side="left", padx=(8, 0))

        self.illustration_model_label = ttk.Label(model_controls, text="Billedmodel")
        self.illustration_model_label.pack(side="left", padx=(14, 6))
        self.illustration_model_picker = ttk.Combobox(
            model_controls, textvariable=self.illustration_model,
            values=tuple(self.available_models), width=17, state="readonly")
        self.illustration_model_picker.pack(side="left")
        self.illustration_model_picker.bind("<<ComboboxSelected>>", self._model_selected)

        ttk.Label(self.ai_panel, text="Afsendelse").grid(row=3, column=0, sticky="w", padx=(0, 12), pady=2)
        workflow = ttk.Frame(self.ai_panel)
        workflow.grid(row=3, column=1, sticky="w")
        self.confirm_picker = ttk.Radiobutton(workflow, variable=self.ai_workflow, value="confirm",
                                              text="Spørg før afsendelse (anbefalet)", command=self._workflow_changed)
        self.confirm_picker.pack(side="left")
        self.auto_picker = ttk.Radiobutton(workflow, variable=self.ai_workflow, value="automatic",
                                           text="Send automatisk efter OCR", command=self._workflow_changed)
        self.auto_picker.pack(side="left", padx=(14, 0))

        ttk.Label(self.ai_panel, text="API-nøgle").grid(row=4, column=0, sticky="w", padx=(0, 12), pady=3)
        key_controls = ttk.Frame(self.ai_panel)
        key_controls.grid(row=4, column=1, sticky="ew", pady=3)
        self.api_key_entry = ttk.Entry(key_controls, textvariable=self.api_key, show="•")
        self.api_key_entry.pack(side="left", fill="x", expand=True)
        control_background = self.colors["window"]
        self.forget_key_button = tk.Button(
            key_controls, text="✕", command=self._forget_key, borderwidth=0, relief="flat",
            highlightthickness=0, foreground=self.colors["danger"],
            activeforeground=self.colors["danger"],
            background=control_background, activebackground=control_background,
            font=("Helvetica", 12, "bold"), cursor="pointinghand", padx=5, pady=0,
            state="normal" if self.has_saved_key else "disabled",
        )
        self.forget_key_button.pack(side="left", padx=(5, 0), fill="y")
        self.save_key_picker = ttk.Checkbutton(key_controls, variable=self.save_key,
                                               text="Gem sikkert i macOS-nøglering")
        self.save_key_picker.pack(side="left", padx=(10, 0))
        self.settings_controls.extend([self.language_picker, self.visuals_picker, self.ai_picker, self.ai_text_picker, self.ai_visuals_picker, self.api_key_entry, self.model_picker, self.illustration_model_picker, self.refresh_models_button])
        self.settings_controls.extend([self.confirm_picker, self.auto_picker, self.save_key_picker])
        self.ai_panel.grid_remove()
        form.columnconfigure(1, weight=1)
        buttons = ttk.Frame(outer)
        buttons.pack(anchor="e", pady=(7, 5))
        self.cancel = ttk.Button(buttons, text="Afbryd", command=self._cancel, state="disabled")
        self.cancel.pack(side="left", padx=(0, 8))
        self.convert = ttk.Button(buttons, text="Opret Kindle-fil…", command=self._start)
        self.convert.pack(side="left")
        status_card = ttk.LabelFrame(outer, text="Status", padding=(12, 7), style="Status.TLabelframe")
        status_card.pack(fill="x")
        self.phase = ttk.Label(status_card, textvariable=self.phase_text, style="Phase.TLabel")
        self.phase.pack(anchor="w")
        self.status = ttk.Label(status_card, text="Klar", style="StatusText.TLabel")
        self.status.pack(anchor="w", pady=(1, 1))
        self.eta = ttk.Label(status_card, textvariable=self.eta_text, style="Eta.TLabel")
        self.eta.pack(anchor="w", pady=(0, 5))
        self.progress = ttk.Progressbar(status_card, mode="determinate")
        self.progress.pack(fill="x")
        self.info = ttk.Label(status_card, textvariable=self.model_help, style="Hint.TLabel", wraplength=620,
                              justify="left", font=("Helvetica", 11))
        self.info.pack(anchor="w", pady=(5, 0))
        self.advanced_panel = ttk.LabelFrame(outer, text="Diagnostik", padding=(12, 8))
        self.advanced_panel.pack(fill="x", pady=(5, 0))
        ttk.Label(self.advanced_panel, text=f"Logfil: {LOG_PATH}", style="Hint.TLabel",
                  wraplength=620).pack(anchor="w")
        diagnostics_buttons = ttk.Frame(self.advanced_panel)
        diagnostics_buttons.pack(anchor="w", pady=(7, 0))
        self.open_log_button = ttk.Button(diagnostics_buttons, text="Åbn log", command=self._open_log)
        self.open_log_button.pack(side="left")
        self.copy_diagnostics_button = ttk.Button(diagnostics_buttons, text="Kopiér diagnostik",
                                                  command=self._copy_diagnostics)
        self.copy_diagnostics_button.pack(side="left", padx=(8, 0))
        self.advanced_panel.pack_forget()
        self.settings_controls.extend([self.developer_link, self.open_log_button, self.copy_diagnostics_button])

    def _row(self, parent, row, label, variable, button=None, command=None):
        ttk.Label(parent, text=label).grid(row=row, column=0, sticky="w", pady=3, padx=(0, 16))
        entry = ttk.Entry(parent, textvariable=variable)
        entry.grid(row=row, column=1, sticky="ew", pady=3)
        self.settings_controls.append(entry)
        if button:
            picker = ttk.Button(parent, text=button, command=command)
            picker.grid(row=row, column=2, padx=(8, 0))
            self.settings_controls.append(picker)

    def _choose(self):
        value = filedialog.askopenfilename(filetypes=[("PDF", "*.pdf")])
        if value:
            self.pdf.set(value)
            if not self.title_value.get():
                self.title_value.set(Path(value).stem.replace("_", " "))

    def _toggle_ai(self):
        if str(self.ai_picker.cget("state")) == "disabled":
            return
        enabled = self.ai_enabled.get()
        if enabled:
            self.ai_panel.grid()
        else:
            self.ai_panel.grid_remove()
        self.api_key_entry.config(state="normal" if enabled else "disabled")
        self.model_picker.config(state="readonly" if enabled else "disabled")
        self.illustration_model_picker.config(
            state="readonly" if enabled and self.ai_illustrations.get() and self.keep_visuals.get() else "disabled")
        self.refresh_models_button.config(state="normal" if enabled and not self.models_loading else "disabled")
        self.ai_text_picker.config(state="normal" if enabled else "disabled")
        self.confirm_picker.config(state="normal" if enabled else "disabled")
        self.auto_picker.config(state="normal" if enabled else "disabled")
        self.ai_visuals_picker.config(state="normal" if enabled and self.keep_visuals.get() else "disabled")
        if not enabled or not self.keep_visuals.get():
            self.ai_illustrations.set(False)
        self._model_selected()
        self._workflow_changed()
        self.after_idle(self._fit_window)

    def _fit_window(self):
        self.update_idletasks()
        available_width = max(640, self.winfo_screenwidth() - 60)
        available_height = max(460, self.winfo_screenheight() - 90)
        width = max(680, min(self.content_frame.winfo_reqwidth() + 18, available_width))
        height = max(480, min(self.content_frame.winfo_reqheight(), available_height))
        self.geometry(f"{width}x{height}")

    def _content_resized(self, _event=None):
        self.canvas.configure(scrollregion=self.canvas.bbox("all"))

    def _canvas_resized(self, event):
        self.canvas.itemconfigure(self.canvas_window, width=max(1, event.width))

    def _mousewheel(self, event):
        if self.canvas.winfo_exists() and self.canvas.winfo_viewable():
            self.canvas.yview_scroll(-1 if event.delta > 0 else 1, "units")

    def _toggle_advanced(self, event=None):
        if self.cancel_event:
            return
        if event is not None:
            self.advanced_enabled.set(not self.advanced_enabled.get())
        if self.advanced_enabled.get():
            self.advanced_panel.pack(fill="x", pady=(5, 0))
            self.developer_link.config(text="Luk Developer")
        else:
            self.advanced_panel.pack_forget()
            self.developer_link.config(text="Developer")
        self.after_idle(self._fit_window)

    def _prevent_sleep(self):
        if self.caffeinate_process is not None:
            return
        try:
            self.caffeinate_process = subprocess.Popen(
                ["/usr/bin/caffeinate", "-i", "-w", str(os.getpid())],
                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            )
            logger.info("Idle system sleep prevention enabled for conversion")
        except OSError:
            logger.exception("Could not start caffeinate; conversion continues without sleep prevention")

    def _allow_sleep(self):
        if self.caffeinate_process is not None:
            self.caffeinate_process.terminate()
            self.caffeinate_process = None
            logger.info("Idle system sleep prevention released")

    def _open_log(self):
        try:
            LOG_PATH.touch(exist_ok=True)
            subprocess.Popen(["/usr/bin/open", str(LOG_PATH)], stdout=subprocess.DEVNULL,
                             stderr=subprocess.DEVNULL)
        except OSError as exc:
            messagebox.showerror("Kunne ikke åbne loggen", str(exc))

    def _copy_diagnostics(self):
        try:
            lines = LOG_PATH.read_text(encoding="utf-8", errors="replace").splitlines()[-120:]
        except OSError:
            lines = ["Logfilen kunne ikke læses."]
        diagnostics = (
            f"PDF til Kindle diagnostik\n"
            f"macOS: {platform.mac_ver()[0]}\n"
            f"Python: {platform.python_version()}\n"
            f"AI-model: {self.ai_model.get()}\n"
            f"Illustrationsmodel: {self.illustration_model.get()}\n"
            f"OCR-sprog: {self.language.get()}\n"
            f"Logfil: {LOG_PATH}\n\nSeneste log:\n" + "\n".join(lines)
        )
        self.clipboard_clear(); self.clipboard_append(diagnostics)
        self.status.config(text="Diagnostik kopieret til udklipsholderen")

    def _workflow_changed(self):
        if not self.ai_enabled.get():
            return
        confirming = self.ai_workflow.get() == "confirm"
        self.model_picker.config(state="disabled" if confirming else "readonly")
        self.model_label.config(text="Model efter OCR" if confirming else "Model")
        if confirming:
            self.model_help.set("Efter OCR får du en sammenligning af modeller, tokens og pris, før noget sendes.")
        else:
            self._model_selected()

    def _model_selected(self, _event=None):
        if not self.ai_enabled.get():
            self.model_help.set("AI er slået fra. Dokumentet behandles kun lokalt.")
            return
        model_name = self.ai_model.get().strip()
        if not model_name:
            self.model_help.set("Klik på ‘Hent modeller’ for at hente aktuelle modeller og anbefalinger.")
            return
        description = self.model_metadata.get(model_name, {}).get(
            "description", "Dynamisk hentet model; yderligere oplysninger er ikke tilgængelige."
        )
        suffix = " Billeder springes over i denne tekst-only-kørsel." if (
            self.ai_cleanup.get() and not self.ai_illustrations.get()) else ""
        self.model_help.set(f"{model_name}: {description}{suffix}")
        self._save_cached_models()

    def _save_cached_models(self):
        if not self.available_models:
            return
        try:
            save_model_cache(
                self.available_models, self.model_metadata,
                self.model_recommendations, self.ai_model.get().strip(),
                self.illustration_model.get().strip(),
            )
        except OSError as exc:
            logger.warning("Could not save model catalog cache: %s", exc)

    def _refresh_models(self):
        api_key = self.api_key.get().strip()
        if not api_key:
            messagebox.showinfo("API-nøgle mangler", "Indtast din OpenAI API-nøgle, før modellerne hentes.")
            return
        if self.models_loading:
            return
        self.models_loading = True
        needs_images = self.ai_illustrations.get() and self.keep_visuals.get()
        self.refresh_models_button.config(state="disabled", text="Henter…")
        self.model_help.set("Henter de modeller, som API-projektet har adgang til…")
        def worker():
            try:
                models = fetch_openai_models(api_key)
                recommendation_error = None
                try:
                    recommendations = recommend_openai_models(
                        api_key, models, needs_images,
                    )
                except Exception as exc:
                    recommendations = []
                    recommendation_error = str(exc)
                    logger.warning("Model recommendation call failed: %s", exc)
                self.events.put(("models_loaded", models, recommendations, recommendation_error))
            except Exception as exc:
                self.events.put(("models_error", str(exc)))
        threading.Thread(target=worker, daemon=True).start()

    def _forget_key(self):
        forget_api_key()
        self.api_key.set("")
        self.has_saved_key = False
        self.forget_key_button.config(state="disabled")
        self.model_help.set("API-nøglen er fjernet fra macOS-nøglering.")

    def _language_selected(self, _event=None):
        code = self.language_codes[self.language.get()]
        if code in available_ocr_languages():
            self.progress["value"] = 100
            self.status.config(text=f"{self.language.get()} er klar")
            self.after(700, lambda: self.progress.config(value=0) if not self.cancel_event else None)
            return
        self.cancel_event = threading.Event()
        self._set_busy("Henter sprog…")
        self.status.config(text=f"Forbereder download af {self.language.get()}…")
        self.progress["value"] = 0
        threading.Thread(target=self._download_language,
                         args=(code, self.language.get(), self.cancel_event), daemon=True).start()

    def _download_language(self, code, language_name, cancel_event):
        try:
            ensure_ocr_language(
                code,
                lambda message, fraction: self.events.put(("language_progress", language_name, fraction)),
                cancel_event,
            )
            self.events.put(("language_done", language_name))
        except ConversionCancelled:
            self.events.put(("cancelled",))
        except Exception as exc:
            self.events.put(("error", str(exc)))

    def _start(self):
        if self.cancel_event:
            return
        source = Path(self.pdf.get())
        if not source.is_file():
            messagebox.showerror("PDF mangler", "Vælg først en PDF-fil.")
            return
        target = filedialog.asksaveasfilename(defaultextension=".epub", initialfile=source.stem + ".epub", filetypes=[("EPUB", "*.epub")])
        if not target:
            return
        use_ai = self.ai_enabled.get() and (self.ai_cleanup.get() or self.ai_illustrations.get())
        if self.ai_enabled.get() and not use_ai:
            messagebox.showerror("Vælg AI-behandling", "Vælg tekst, billeder eller begge dele i AI-panelet.")
            return
        if use_ai and not self.api_key.get().strip():
            messagebox.showerror("API-nøgle mangler", "Indtast din OpenAI API-nøgle, eller slå OpenAI-funktionerne fra.")
            return
        if use_ai and not self.ai_model.get().strip():
            messagebox.showerror("AI-model mangler", "Klik på ‘Opdatér modeller’ og vælg en tilgængelig model først.")
            return
        if use_ai and self.ai_illustrations.get() and not self.illustration_model.get().strip():
            messagebox.showerror("Illustrationsmodel mangler", "Vælg en model til billedverifikation.")
            return
        if use_ai and self.save_key.get():
            try:
                save_api_key(self.api_key.get().strip())
                self.has_saved_key = True
            except RuntimeError as exc:
                messagebox.showwarning("Kunne ikke gemme API-nøglen", str(exc))
        self._set_busy("Konverterer…")
        self._prevent_sleep()
        self.cancel_event = threading.Event()
        self.started_at = time.monotonic()
        self.eta_text.set("Beregner samlet tidsestimat…")
        self.run_warning = None
        self.require_ai_confirmation = self.ai_workflow.get() == "confirm"
        self.progress["value"] = 0
        self.status.config(text="Starter konvertering…")
        self.phase_text.set("Lokal behandling")
        language_code = self.language_codes[self.language.get()]
        process_images = self.keep_visuals.get() and (
            not self.ai_enabled.get() or (use_ai and self.ai_illustrations.get()))
        options = ConversionOptions(
            self.title_value.get().strip() or source.stem,
            self.author.get().strip(), language_code,
            preserve_visual_pages=process_images,
            ai_cleanup=use_ai and self.ai_cleanup.get(), api_key=self.api_key.get().strip(),
            ai_illustrations=use_ai and self.ai_illustrations.get(),
            ai_model=self.ai_model.get().strip(),
            illustration_model=self.illustration_model.get().strip(),
        )
        threading.Thread(target=self._worker, args=(source, Path(target), options, self.cancel_event), daemon=True).start()

    def _worker(self, source, target, options, cancel_event):
        logger.info("Worker started: source=%s target=%s", source.name, target.name)
        try:
            convert_pdf_to_epub(source, target, options,
                                lambda m, f, eta=None: self.events.put(("progress", m, f, eta)), cancel_event,
                                self._approve_ai, lambda message: self.events.put(("warning", message)))
            validate_epub(target)
            self.events.put(("done", target))
        except ConversionCancelled:
            logger.info("Conversion cancelled by user")
            self.events.put(("cancelled",))
        except Exception as exc:
            logger.exception("Conversion failed")
            self.events.put(("error", str(exc)))

    def _approve_ai(self, input_tokens, output_tokens, model, image_pages=0,
                    vision_input_tokens=0, vision_output_tokens=0, illustration_model=""):
        if not self.require_ai_confirmation:
            self.events.put(("ai_auto_estimate", input_tokens, output_tokens, model, image_pages,
                             vision_input_tokens, vision_output_tokens, illustration_model))
            return model
        response_ready = threading.Event()
        decision = {"model": None}
        self.events.put(("ai_estimate", input_tokens, output_tokens, model, image_pages,
                         vision_input_tokens, vision_output_tokens, illustration_model,
                         response_ready, decision))
        while not response_ready.wait(0.1):
            if self.cancel_event and self.cancel_event.is_set():
                return False
        return decision["model"]

    def _estimated_price(self, model, input_tokens, output_tokens):
        if input_tokens <= 0 and output_tokens <= 0:
            return 0.0
        metadata = self.model_metadata.get(model, {})
        input_price = metadata.get("input_price")
        output_price = metadata.get("output_price")
        if input_price is None or output_price is None:
            return None
        return input_tokens / 1_000_000 * input_price + output_tokens / 1_000_000 * output_price

    @staticmethod
    def _price_text(price):
        if price is None:
            return "Pris ukendt"
        if price < 0.001:
            return "< $0,001"
        return f"$ {price:.3f}".replace(".", ",")

    def _model_dialog(self, input_tokens, output_tokens, current_model, image_pages,
                      vision_input_tokens=0, vision_output_tokens=0, illustration_model=""):
        dialog = tk.Toplevel(self)
        dialog.title("Vælg AI-model")
        dialog.resizable(False, False)
        dialog.transient(self)
        dialog.grab_set()
        recommendations = list(self.model_recommendations)
        if not recommendations:
            recommendations = [
                {"model_id": name, "profile": "Tilgængelig model",
                 "reason": self.model_metadata.get(name, {}).get("description", "Kan bruges til dokumentbehandlingen.")}
                for name in self.available_models[:3]
            ]
        if not recommendations:
            messagebox.showerror("Ingen modeller", "Klik på ‘Opdatér modeller’ i AI-indstillingerne først.")
            dialog.destroy()
            return None
        models = [item["model_id"] for item in recommendations]
        selected = tk.StringVar(value=current_model if current_model in models else models[0])
        result = {"model": None}
        frame = ttk.Frame(dialog, padding=22)
        frame.pack(fill="both", expand=True)
        ttk.Label(frame, text="Vælg model før afsendelse", font=("Helvetica", 17, "bold")).grid(
            row=0, column=0, columnspan=4, sticky="w")
        total_tokens = input_tokens + output_tokens
        detail = (f"Samlet estimeret forbrug: {total_tokens:,} tokens "
                  f"({input_tokens:,} input + {output_tokens:,} output)")
        if image_pages:
            detail += f" · kandidat-crops fra {image_pages} sider"
        ttk.Label(frame, text=detail.replace(",", "."), style="Hint.TLabel").grid(
            row=1, column=0, columnspan=4, sticky="w", pady=(4, 16))
        for column, label in enumerate(("Model", "Anbefaling", "Estimeret pris")):
            ttk.Label(frame, text=label, font=("Helvetica", 11, "bold")).grid(
                row=2, column=column, sticky="w", padx=(0, 22), pady=(0, 6))
        row = 3
        for recommendation in recommendations:
            model_name = recommendation["model_id"]
            label = model_name
            ttk.Radiobutton(frame, text=label, variable=selected, value=model_name).grid(
                row=row, column=0, sticky="w", padx=(0, 22), pady=6)
            ttk.Label(frame, text=recommendation.get("profile", "Anbefalet")).grid(row=row, column=1, sticky="w")
            text_input = max(0, input_tokens-vision_input_tokens)
            text_output = max(0, output_tokens-vision_output_tokens)
            text_price = self._estimated_price(model_name, text_input, text_output)
            vision_price = self._estimated_price(
                illustration_model, vision_input_tokens, vision_output_tokens)
            price = None if text_price is None or (image_pages and vision_price is None) else (
                text_price + (vision_price or 0))
            ttk.Label(frame, text=self._price_text(price)).grid(row=row, column=2, sticky="w")
            ttk.Label(frame, text=recommendation.get("reason", ""), style="Hint.TLabel",
                      wraplength=560, justify="left").grid(
                          row=row + 1, column=0, columnspan=3, sticky="w", padx=(28, 0), pady=(0, 8))
            row += 2
        catalog_models = list(dict.fromkeys(self.available_models or models))
        show_all_button = ttk.Button(frame, text="Vælg blandt alle modeller…")
        show_all_button.grid(row=row, column=0, columnspan=3, sticky="w", pady=(4, 8))
        all_models_frame = ttk.Frame(frame)
        all_models_frame.grid(row=row + 1, column=0, columnspan=3, sticky="ew", pady=(0, 10))
        ttk.Label(all_models_frame, text="Alle tilgængelige modeller").pack(side="left", padx=(0, 10))
        all_model = tk.StringVar(value=selected.get())
        all_model_picker = ttk.Combobox(
            all_models_frame, textvariable=all_model, values=tuple(catalog_models),
            state="readonly", width=27)
        all_model_picker.pack(side="left")
        def select_all_model(_event=None):
            if all_model.get():
                selected.set(all_model.get())
        all_model_picker.bind("<<ComboboxSelected>>", select_all_model)
        all_models_frame.grid_remove()
        def toggle_all_models():
            if all_models_frame.winfo_ismapped():
                all_models_frame.grid_remove()
                show_all_button.config(text="Vælg blandt alle modeller…")
            else:
                all_model.set(selected.get())
                all_models_frame.grid()
                show_all_button.config(text="Skjul alle modeller")
            dialog.update_idletasks()
        show_all_button.config(command=toggle_all_models)
        selected_help = tk.StringVar()
        def update_help(*_):
            description = self.model_metadata.get(selected.get(), {}).get(
                "description", "Dynamisk hentet model; yderligere oplysninger er ikke tilgængelige.")
            text_price = self._estimated_price(
                selected.get(), max(0, input_tokens-vision_input_tokens),
                max(0, output_tokens-vision_output_tokens))
            vision_price = self._estimated_price(
                illustration_model, vision_input_tokens, vision_output_tokens)
            total_price = None if text_price is None or (image_pages and vision_price is None) else (
                text_price + (vision_price or 0))
            selected_help.set(
                f"Valgt: {selected.get()} · {self._price_text(total_price)} · {description}")
        selected.trace_add("write", update_help)
        update_help()
        footer_row = row + 2
        ttk.Label(frame, textvariable=selected_help, style="Hint.TLabel", wraplength=590).grid(
            row=footer_row, column=0, columnspan=3, sticky="w", pady=(8, 2))
        ttk.Label(frame, text="Estimatet er vejledende. Den faktiske pris bestemmes af API'ets målte tokenforbrug.",
                  style="Hint.TLabel").grid(row=footer_row + 1, column=0, columnspan=3, sticky="w", pady=(2, 16))
        buttons = ttk.Frame(frame)
        buttons.grid(row=footer_row + 2, column=0, columnspan=3, sticky="e")
        def close(value=None):
            result["model"] = value
            dialog.destroy()
        ttk.Button(buttons, text="Annuller", command=close).pack(side="left", padx=(0, 8))
        ttk.Button(buttons, text="Brug valgte model og send",
                   command=lambda: close(selected.get())).pack(side="left")
        dialog.protocol("WM_DELETE_WINDOW", close)
        dialog.wait_visibility()
        dialog.focus_force()
        dialog.wait_window()
        return result["model"]

    def _cancel(self):
        if self.cancel_event:
            self.cancel_event.set()
            self.cancel.config(state="disabled")
            self.status.config(text="Afbryder…")
            self.phase_text.set("Stopper processen")

    def _on_close(self):
        if self.cancel_event:
            if messagebox.askyesno("Afbryd processen?",
                                   "En proces er stadig i gang. Vil du afbryde den og lukke appen?"):
                self.closing_requested = True
                self._cancel()
            return
        self.destroy()

    @staticmethod
    def _duration(seconds):
        seconds = max(0, round(seconds))
        minutes, seconds = divmod(seconds, 60)
        if minutes:
            return f"ca. {minutes} min. {seconds} sek."
        return f"ca. {seconds} sek."

    def _set_busy(self, button_text="Konverterer…"):
        self.convert.config(text=button_text)
        self.convert.state(["disabled"])
        for control in self.settings_controls:
            control.state(["disabled"])
        self.refresh_models_button.config(state="disabled")
        self.forget_key_button.config(state="disabled")
        self.cancel.state(["!disabled"])

    def _idle(self):
        self._allow_sleep()
        self.convert.config(text="Opret Kindle-fil…")
        self.convert.state(["!disabled"])
        for control in self.settings_controls:
            control.state(["!disabled"])
        self.language_picker.config(state="readonly")
        self._toggle_ai()
        self.forget_key_button.config(state="normal" if self.ai_enabled.get() and self.has_saved_key else "disabled")
        self.cancel.config(state="disabled")
        self.cancel_event = None
        self.started_at = None
        self.cloud_wait_started = None
        self.cloud_wait_prefix = ""

    def _poll(self):
        try:
            while True:
                event = self.events.get_nowait()
                if event[0] == "progress":
                    if self.cancel_event:
                        self.convert.state(["disabled"])
                    fraction = event[2]
                    explicit_eta = event[3] if len(event) > 3 else None
                    label = event[1]
                    wait_match = re.match(r"^(.*· )(\d+) sek\. gået$", label)
                    if wait_match:
                        prefix, reported_seconds = wait_match.group(1), int(wait_match.group(2))
                        if prefix != self.cloud_wait_prefix:
                            self.cloud_wait_prefix = prefix
                            self.cloud_wait_started = time.monotonic() - reported_seconds
                    else:
                        self.cloud_wait_started = None
                        self.cloud_wait_prefix = ""
                    logger.debug("Progress %.1f%%: %s", fraction * 100, label)
                    cloud_status = (label.startswith("Sender ") or label.startswith("AI ") or
                                    "Venter på OpenAI" in label or "Midlertidig OpenAI" in label or
                                    label.startswith("Tekstportion ") or label.startswith("Billedside "))
                    if cloud_status:
                        self.phase_text.set("Behandles med OpenAI API")
                        self.model_help.set("Data behandles nu via OpenAI API. Appen venter på svar og fortsætter automatisk.")
                    elif label.startswith("Bygger EPUB"):
                        self.phase_text.set("Opretter Kindle-fil lokalt")
                    elif label != "Færdig":
                        self.phase_text.set("Lokal behandling")
                    if explicit_eta is not None and fraction < 1:
                        self.eta_text.set(f"Forventet tid tilbage: {self._duration(explicit_eta)}")
                    elif len(event) > 3 and fraction < 1:
                        self.eta_text.set("Beregner et pålideligt tidsestimat…")
                    elif self.started_at and 0.04 < fraction < 0.99:
                        elapsed = time.monotonic() - self.started_at
                        remaining = elapsed * (1 - fraction) / fraction
                        self.eta_text.set(f"Forventet tid tilbage: {self._duration(remaining)}")
                    elif fraction >= 1:
                        self.eta_text.set("Færdig")
                    self.status.config(text=label); self.progress["value"] = fraction * 100
                elif event[0] == "language_progress":
                    self.phase_text.set("Henter OCR-sprog")
                    self.status.config(text=f"Henter {event[1]}… {round(event[2] * 100)} %")
                    self.progress["value"] = event[2] * 100
                elif event[0] == "language_done":
                    self._idle(); self.progress["value"] = 100
                    self.phase_text.set("Klar")
                    self.status.config(text=f"{event[1]} er downloadet og klar")
                elif event[0] == "ai_estimate":
                    self.phase_text.set("Afventer din godkendelse")
                    (input_tokens, output_tokens, model, image_pages, vision_input, vision_output,
                     illustration_model, response_ready, decision) = event[1:]
                    total = input_tokens + output_tokens
                    selected_model = self._model_dialog(
                        input_tokens, output_tokens, model, image_pages,
                        vision_input, vision_output, illustration_model)
                    decision["model"] = selected_model
                    response_ready.set()
                    if selected_model:
                        self.ai_model.set(selected_model)
                        self._save_cached_models()
                        text_price = self._estimated_price(
                            selected_model, input_tokens-vision_input, output_tokens-vision_output)
                        vision_price = self._estimated_price(
                            illustration_model, vision_input, vision_output)
                        price = None if text_price is None or (image_pages and vision_price is None) else (
                            text_price + (vision_price or 0))
                        self.model_help.set(
                            f"Godkendt: ca. {total:,} tokens med {selected_model} · {self._price_text(price)}"
                            + (f"; billed-crops verificeres med {illustration_model}." if image_pages else ".")
                        )
                        self.phase_text.set("Behandles med OpenAI API")
                elif event[0] == "ai_auto_estimate":
                    (input_tokens, output_tokens, model, image_pages, _vision_input, _vision_output,
                     illustration_model) = event[1:]
                    total = input_tokens + output_tokens
                    self.model_help.set(
                        f"Automatisk afsendelse: ca. {total:,} tokens med {model}"
                        + (f"; kandidat-crops fra {image_pages} sider verificeres med {illustration_model}."
                           if image_pages else ".")
                    )
                    self.phase_text.set("Behandles med OpenAI API")
                elif event[0] == "warning":
                    self.cloud_wait_started = None
                    self.cloud_wait_prefix = ""
                    self.run_warning = (f"{self.run_warning}\n\n{event[1]}" if self.run_warning else event[1])
                    self.phase_text.set("Fortsætter med sikker reserveplan")
                    self.model_help.set(
                        "Færdige AI-resultater bevares. Kun berørte dele bruger det lokale OCR-resultat."
                    )
                elif event[0] == "models_loaded":
                    self.models_loading = False
                    self.refresh_models_button.config(
                        text="Opdatér modeller", state="disabled" if self.cancel_event else "normal")
                    models = event[1]
                    recommendations = list(event[2]) if len(event) > 2 else []
                    recommendation_error = event[3] if len(event) > 3 else None
                    if models:
                        self.available_models = models
                        self.model_picker.config(values=tuple(models))
                        self.illustration_model_picker.config(values=tuple(models))
                        self.model_metadata = {}
                        for item in recommendations:
                            model_id = item.get("model_id")
                            if model_id:
                                self.model_metadata[model_id] = {
                                    "description": item.get("description") or item.get("reason", ""),
                                    "input_price": item.get("input_price"),
                                    "output_price": item.get("output_price"),
                                }
                        used = {item.get("model_id") for item in recommendations}
                        fallback_profiles = ("Bedste balance", "Laveste pris", "Højeste kvalitet")
                        for model_name in models:
                            if len(recommendations) >= 3:
                                break
                            if model_name not in used:
                                recommendations.append({
                                    "model_id": model_name,
                                    "profile": fallback_profiles[len(recommendations)],
                                    "reason": "Tilgængelig for dit API-projekt; metadata kunne ikke bekræftes.",
                                })
                                used.add(model_name)
                        self.model_recommendations = recommendations[:3]
                        recommended_ids = [item["model_id"] for item in self.model_recommendations]
                        if self.ai_model.get() not in models:
                            self.ai_model.set(recommended_ids[0] if recommended_ids else models[0])
                        if self.illustration_model.get() not in models:
                            self.illustration_model.set(
                                "gpt-5.6-luna" if "gpt-5.6-luna" in models
                                else (recommended_ids[0] if recommended_ids else models[0]))
                        self.models_loaded = True
                        self._save_cached_models()
                        suffix = " Anbefalingerne blev lavet lokalt, fordi baggrundskaldet fejlede." if recommendation_error else " Tre modeller er anbefalet til opgaven."
                        self.model_help.set(f"{len(models)} tilgængelige OpenAI-modeller hentet.{suffix}")
                    else:
                        self.model_help.set("OpenAI returnerede ingen egnede tekst- og billedmodeller. Reservelisten beholdes.")
                elif event[0] == "models_error":
                    self.models_loading = False
                    self.refresh_models_button.config(
                        text="Prøv igen", state="disabled" if self.cancel_event else "normal")
                    self.model_help.set(f"Kunne ikke hente modeller. Reservelisten bruges. {event[1]}")
                elif event[0] == "done":
                    self._idle(); self.phase_text.set("Færdig"); self.status.config(text="Kindle-filen er oprettet"); self.eta_text.set("Færdig")
                    if self.closing_requested:
                        self.destroy(); return
                    if self.run_warning:
                        messagebox.showwarning(
                            "Færdig med bemærkning",
                            f"Kindle-filen er gemt som:\n{event[1]}\n\nBemærkning fra AI-behandlingen:\n{self.run_warning}",
                        )
                    else:
                        messagebox.showinfo("Færdig", f"Kindle-filen er gemt som:\n{event[1]}")
                elif event[0] == "cancelled":
                    self._idle(); self.progress["value"] = 0; self.phase_text.set("Afbrudt"); self.status.config(text="Konverteringen blev afbrudt"); self.eta_text.set("")
                    if self.closing_requested:
                        self.destroy(); return
                else:
                    self._idle(); self.phase_text.set("Fejl"); self.status.config(text="Processen stoppede"); self.eta_text.set("")
                    if self.closing_requested:
                        self.destroy(); return
                    messagebox.showerror("Konverteringen fejlede", event[1])
        except queue.Empty:
            pass
        if self.cancel_event and self.cloud_wait_started is not None and self.cloud_wait_prefix:
            elapsed = max(0, int(time.monotonic() - self.cloud_wait_started))
            self.status.config(text=f"{self.cloud_wait_prefix}{elapsed} sek. gået")
            self.refresh_models_button.config(state="disabled")
        self.after(100, self._poll)


if __name__ == "__main__":
    if os.environ.get("PDF_TO_KINDLE_SELF_TEST") == "1":
        import kindle_converter as converter
        print(json.dumps({
            "frozen": bool(getattr(__import__("sys"), "frozen", False)),
            "tesseract": converter._tool("tesseract"),
            "ocr_languages": converter.available_ocr_languages(),
            "pymupdf": converter.fitz is not None,
        }, ensure_ascii=False))
    else:
        App().mainloop()
