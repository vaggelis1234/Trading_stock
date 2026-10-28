"""Trading Bot window. Stage-1 controls only; buy execution stays unavailable."""

from __future__ import annotations

from datetime import datetime
import logging
import socket
import subprocess
import sys
import threading
import time
import tkinter as tk
import webbrowser
from pathlib import Path
from tkinter import ttk
from tkinter.scrolledtext import ScrolledText

from src.control.cli import format_help
from src.control.instance import AlreadyRunning
from src.control.permissions import LaunchOptions, resolve_buy_permission
from src.control.service import ControlService
from src.control.status import current_status

logger = logging.getLogger(__name__)

ROOT = Path(__file__).resolve().parents[2]
API_PORT = 8000
WEB_UI_PORT = 5173
WEB_UI_URL = f"http://127.0.0.1:{WEB_UI_PORT}"


def buy_checkbox_state(enable_scheduler: bool, enable_watch_engine: bool, requested: bool) -> tuple[bool, str]:
    """``yes_buy`` can be requested only while both services are selected."""
    allowed = bool(enable_scheduler) and bool(enable_watch_engine)
    return bool(requested) and allowed, "normal" if allowed else "disabled"


def universe_activity_line(enable_scheduler: bool, enable_watch_engine: bool, *, repeat: bool) -> str:
    """One dialog line for a Universe start. Names only the services that are on."""
    action = "new scan universe" if repeat else "start universe"
    if enable_scheduler and enable_watch_engine:
        return f"{action} with scheduler and watch engine"
    if enable_scheduler:
        return f"{action} with scheduler"
    if enable_watch_engine:
        return f"{action} with watch engine"
    return action


def web_ui_is_up(port: int = WEB_UI_PORT) -> bool:
    try:
        with socket.create_connection(("127.0.0.1", port), timeout=0.3):
            return True
    except OSError:
        return False


def launch_web_ui(
    *,
    root: Path = ROOT,
    port_open=None,
    api_port_open=None,
    popen=None,
    open_browser=None,
    python: str | None = None,
    vite_command=None,
) -> str:
    """Open the React Universe page. Starts the API and Vite when they are down."""
    opener = open_browser or webbrowser.open
    spawn = popen or subprocess.Popen
    ui_up = (port_open or web_ui_is_up)()
    api_up = (api_port_open or (lambda: web_ui_is_up(API_PORT)))()
    started = False
    if not api_up:
        _spawn(
            spawn,
            [
                python or _console_python(),
                "-m",
                "uvicorn",
                "src.ui.app:app",
                "--host",
                "127.0.0.1",
                "--port",
                str(API_PORT),
            ],
            root,
        )
        started = True
    if not ui_up:
        _spawn(spawn, vite_command or _vite_command(root), root / "web")
        started = True
    if started:
        return "started"
    opener(WEB_UI_URL)
    return "opened"


def _vite_command(root: Path) -> list[str]:
    web = root / "web"
    name = "vite.cmd" if sys.platform == "win32" else "vite"
    local = web / "node_modules" / ".bin" / name
    if local.exists():
        return [str(local), "--host", "127.0.0.1", "--port", str(WEB_UI_PORT)]
    npm = "npm.cmd" if sys.platform == "win32" else "npm"
    return [npm, "run", "dev"]


def _spawn(popen, command: list[str], cwd: Path) -> None:
    kwargs = {"cwd": str(cwd)}
    if sys.platform == "win32":
        kwargs["creationflags"] = getattr(subprocess, "CREATE_NO_WINDOW", 0)
    popen(command, **kwargs)


def _console_python() -> str:
    executable = Path(sys.executable)
    if executable.name.lower() == "pythonw.exe":
        console = executable.with_name("python.exe")
        if console.exists():
            return str(console)
    return str(executable)


class TradingBotApp:
    def __init__(self, root: tk.Tk | None = None, service_factory=None, status_reader=None, web_opener=None):
        self.root = root or tk.Tk()
        self.root.title("Trading Bot")
        self.service_factory = service_factory or ControlService
        self.status_reader = status_reader or current_status
        self.web_opener = web_opener or launch_web_ui
        self.service = None
        self._active_options = None
        self._scans_started = 0
        self._web_process = None
        self._web_starting = False
        self.worker = None
        self._closed = threading.Event()
        self.help_window = None
        self.status_window = None
        self.help_text = None
        self.status_text = None
        self.scheduler_var = tk.BooleanVar(value=False)
        self.watch_var = tk.BooleanVar(value=False)
        self.buy_var = tk.BooleanVar(value=False)
        self._build()
        self.root.protocol("WM_DELETE_WINDOW", self.close)

    def run(self) -> None:
        self.root.mainloop()

    def _build(self) -> None:
        frame = ttk.Frame(self.root, padding=16)
        frame.grid(sticky="nsew")
        self.root.columnconfigure(0, weight=1)
        self.root.rowconfigure(0, weight=1)
        frame.columnconfigure(0, weight=1)

        actions = ttk.Frame(frame)
        actions.grid(row=0, column=0, sticky="ew", pady=(0, 12))
        actions.columnconfigure(0, weight=1)
        actions.columnconfigure(1, weight=1)
        self.start_button = ttk.Button(actions, text="START_UNIVERSE", command=self.start_universe)
        self.start_button.grid(row=0, column=0, sticky="ew", padx=(0, 8))
        self.stop_button = ttk.Button(actions, text="STOP", command=self.stop_universe, state="disabled")
        self.stop_button.grid(row=0, column=1, sticky="ew")

        self.scheduler_check = ttk.Checkbutton(
            frame,
            text="Enable Scheduler",
            variable=self.scheduler_var,
            command=self._sync_buy,
        )
        self.scheduler_check.grid(row=1, column=0, sticky="w")
        self.watch_check = ttk.Checkbutton(
            frame,
            text="Enable Watch Engine",
            variable=self.watch_var,
            command=self._sync_buy,
        )
        self.watch_check.grid(row=2, column=0, sticky="w")
        self.buy_check = ttk.Checkbutton(
            frame,
            text="Yes, Allow Buys",
            variable=self.buy_var,
            command=self._sync_buy,
        )
        self.buy_check.grid(row=3, column=0, sticky="w", pady=(0, 8))
        self._sync_buy()

        note = ttk.Label(
            frame,
            text="Buy execution is not implemented. Yes, Allow Buys only records approval.",
            wraplength=420,
        )
        note.grid(row=4, column=0, sticky="w", pady=(0, 12))

        buttons = ttk.Frame(frame)
        buttons.grid(row=5, column=0, sticky="ew", pady=(0, 12))
        ttk.Button(buttons, text="HELP", command=self.show_help).grid(row=0, column=0, padx=(0, 8))
        ttk.Button(buttons, text="STATUS", command=self.show_status).grid(row=0, column=1, padx=(0, 8))
        ttk.Button(buttons, text="WEB UI", command=self.open_web_ui).grid(row=0, column=2)

        dialog = ttk.LabelFrame(frame, text="Universe", padding=8)
        dialog.grid(row=6, column=0, sticky="nsew")
        dialog.columnconfigure(0, weight=1)
        dialog.rowconfigure(0, weight=1)
        frame.rowconfigure(6, weight=1)
        self.activity = ScrolledText(dialog, height=8, width=64, state="disabled")
        self.activity.grid(row=0, column=0, columnspan=2, sticky="nsew")
        self.progress = ttk.Progressbar(dialog, orient="horizontal", mode="determinate", maximum=100)
        self.progress.grid(row=1, column=0, sticky="ew", padx=(0, 8), pady=(8, 0))
        self.percent_var = tk.StringVar(value="0")
        ttk.Label(dialog, textvariable=self.percent_var, width=3).grid(row=1, column=1, pady=(8, 0))
        self._set_progress(0)

    def start_universe(self) -> None:
        if self.worker is not None and self.worker.is_alive():
            self._append("Trading Bot is already running")
            return
        try:
            permission = resolve_buy_permission(
                enable_scheduler=self.scheduler_var.get(),
                enable_watch_engine=self.watch_var.get(),
                yes_buy=self.buy_var.get(),
            )
        except Exception as exc:
            self.buy_var.set(False)
            self._sync_buy()
            self._append(str(exc))
            return
        options = LaunchOptions(
            enable_scheduler=self.scheduler_var.get(),
            enable_watch_engine=self.watch_var.get(),
            buy_permission=permission,
        )
        self._active_options = options
        self._scans_started += 1
        self._log(universe_activity_line(
            options.enable_scheduler,
            options.enable_watch_engine,
            repeat=self._scans_started > 1,
        ))
        self._set_progress(0)
        self._set_running(True)
        self.worker = threading.Thread(target=self._run_service, args=(options,), name="trading-bot", daemon=False)
        self.worker.start()

    def stop_universe(self) -> None:
        """Stop the running Universe scan and its scheduler. The window stays open."""
        service = self.service
        if service is None or self.worker is None or not self.worker.is_alive():
            self._append("Universe is not running")
            return
        self._log("universe scan stopped")
        self.stop_button.configure(state="disabled")
        try:
            request = getattr(service, "request_stop", None)
            if request is not None:
                request()
            else:
                service.stop()
        except Exception as exc:
            logger.exception("stop failed")
            self._append(f"Stop failed: {exc}")

    def show_help(self) -> None:
        window, widget = _text_window(self.root, "Help", format_help())
        self.help_window = window
        self.help_text = widget

    def show_status(self) -> None:
        report = self.status_reader()
        body = "This view does not start or change services.\n\n" + "\n".join(report.lines())
        window, widget = _text_window(self.root, "Status", body)
        self.status_window = window
        self.status_text = widget

    def open_web_ui(self) -> None:
        if self._web_starting:
            return
        self._web_starting = True
        try:
            result = self.web_opener()
        except Exception as exc:
            self._web_starting = False
            logger.exception("web ui failed")
            self._log(f"web ui failed: {exc}")
            return
        if result == "started":
            self._log("web ui starting")
            threading.Thread(target=self._open_web_when_ready, name="web-ui", daemon=True).start()
            return
        self._web_starting = False
        self._log("web ui opened")

    def close(self) -> None:
        self._closed.set()
        self._append("Stopping Trading Bot")
        service = self.service
        if service is not None:
            try:
                request = getattr(service, "request_stop", None)
                if request is not None:
                    request()
                else:
                    service.stop()
            except Exception:
                logger.exception("stop failed while closing the window")
        self.root.destroy()

    def _run_service(self, options: LaunchOptions) -> None:
        service = self.service_factory(
            options,
            on_event=self._thread_event,
            on_progress=self._on_progress,
        )
        self.service = service
        try:
            if self._closed.is_set():
                return
            service.start()
            if self._closed.is_set():
                return
            if service.loops_active:
                service.wait()
        except AlreadyRunning as exc:
            self._thread_event(str(exc))
        except Exception as exc:
            logger.exception("Trading Bot stopped because startup failed")
            self._thread_event(f"Startup failed: {exc}")
        finally:
            try:
                service.stop()
            except Exception:
                logger.exception("stop failed")
            self.service = None
            self._post(lambda: self._set_running(False))

    def _sync_buy(self) -> None:
        checked, state = buy_checkbox_state(
            self.scheduler_var.get(),
            self.watch_var.get(),
            self.buy_var.get(),
        )
        self.buy_var.set(checked)
        self.buy_check.configure(state=state)

    def _set_running(self, running: bool) -> None:
        state = "disabled" if running else "normal"
        self.start_button.configure(state=state)
        self.stop_button.configure(state="normal" if running else "disabled")
        self.scheduler_check.configure(state=state)
        self.watch_check.configure(state=state)
        if running:
            self.buy_check.configure(state="disabled")
        else:
            self._sync_buy()

    def _open_web_when_ready(self) -> None:
        try:
            for _ in range(60):
                if web_ui_is_up() and web_ui_is_up(API_PORT):
                    webbrowser.open(WEB_UI_URL)
                    self._post(lambda: self._log("web ui opened"))
                    return
                time.sleep(0.5)
            self._post(lambda: self._log("web ui failed to open"))
        finally:
            self._web_starting = False

    def _thread_event(self, message: str) -> None:
        if message == "Universe scan stopped":
            self._post(lambda: self._log("universe scan stopped", once=True))
        elif message == "Universe scan succeeded":
            self._post(lambda: self._log("universe scan finished"))
        elif message == "Universe scan failed" or message.startswith("Universe scan failed:"):
            self._post(lambda: self._log("universe scan failed"))
        elif message == "scheduled universe scan":
            self._post(self._log_scheduled_scan)
        else:
            logger.info("%s", message)

    def _log_scheduled_scan(self) -> None:
        options = self._active_options
        if options is None:
            self._log("new scan universe")
            return
        self._set_progress(0)
        self._log(universe_activity_line(
            options.enable_scheduler,
            options.enable_watch_engine,
            repeat=True,
        ))

    def _on_progress(self, percent: int) -> None:
        self._post(lambda: self._set_progress(percent))

    def _set_progress(self, percent: int) -> None:
        value = max(0, min(100, int(percent)))
        self.progress["value"] = value
        self.percent_var.set(str(value))

    def _post(self, callback) -> None:
        try:
            self.root.after(0, callback)
        except RuntimeError:
            return

    def _append(self, message: str) -> None:
        logger.info("%s", message)

    def _log(self, text: str, *, once: bool = False) -> None:
        if once and self.activity.get("1.0", "end").rstrip().endswith(text):
            return
        stamp = datetime.now().strftime("%H:%M:%S")
        self.activity.configure(state="normal")
        self.activity.insert("end", f"{stamp} : {text}\n")
        self.activity.see("end")
        self.activity.configure(state="disabled")


def _text_window(root: tk.Misc, title: str, body: str) -> tuple[tk.Toplevel, ScrolledText]:
    window = tk.Toplevel(root)
    window.title(title)
    widget = ScrolledText(window, width=78, height=24)
    widget.grid(sticky="nsew", padx=8, pady=8)
    window.columnconfigure(0, weight=1)
    window.rowconfigure(0, weight=1)
    widget.insert("1.0", body)
    widget.configure(state="disabled")
    return window, widget
