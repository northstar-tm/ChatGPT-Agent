"""Modern CustomTkinter interface for the ChatGPT agent (launched with --gui)."""

import json
import os
import queue
import threading
import time
from argparse import Namespace
from datetime import datetime
from difflib import SequenceMatcher
from tkinter import filedialog

import customtkinter as ctk

import agent
from agent import AUDIT, WORKSPACE, ChatUI

BG, SIDEBAR, CARD, CARD2 = "#0f1117", "#151821", "#1b1f2b", "#242938"
TEXT, MUTED = "#e8eaf0", "#8a90a6"
ACCENT, ACCENT_HOVER = "#7c5cff", "#6a49f0"
GREEN, RED, YELLOW = "#34d399", "#f87171", "#fbbf24"
STATUS_COLORS = {"ok": GREEN, "refused": YELLOW, "error": RED}
FONT = "Segoe UI"


class GuiUI(ChatUI):
    """ChatUI whose output goes to the window; called from the worker thread."""

    def __init__(self, app: "App") -> None:
        super().__init__()
        self.app = app

    def start(self) -> None:
        pass

    def user(self, message: str) -> None:
        self.app.post(self.app.add_bubble, "user", message)

    def assistant(self, message: str) -> None:
        self.app.post(self.app.add_bubble, "assistant", message)

    def notice(self, message: str) -> None:
        self.app.post(self.app.add_bubble, "notice", message)
        self.app.post(self.app.status.set, message)

    def action(self, name: str, arg: str, status: str, detail: str) -> None:
        self.app.post(self.app.add_action, name, arg, status, detail)

    def change_bar(self) -> None:
        added, removed = self.change_totals()
        self.app.post(self.app.show_changes, dict(self.file_states), added, removed)

    def audit(self, entries: list[dict]) -> None:
        self.app.post(self.app.show_audit, [dict(e) for e in entries])

    def pause(self, message: str) -> None:
        self.app.call_main(self.app.dialog, "GPT Agent", message, False)

    def confirm(self, question: str) -> bool:
        return self.app.call_main(self.app.dialog, "Confirm action", question.strip(), True)

    def prompt(self, label: str) -> str:
        if self.app.stop_requested.is_set():
            return "/quit" if "/quit" in label else "stop"
        self.app.post(self.app.begin_input, label)
        return self.app.answer.get()


class App:
    def __init__(self, root: ctk.CTk) -> None:
        self.root = root
        self.calls: queue.Queue = queue.Queue()
        self.stop_requested = threading.Event()
        self.worker: threading.Thread | None = None
        self.answer: queue.Queue = queue.Queue()
        self.waiting = False
        self.ui = GuiUI(self)
        self.mode = ctk.StringVar(value="Off-screen")
        self.status = ctk.StringVar(value="Ready")
        self.bubble_labels: list[ctk.CTkLabel] = []
        self.build()
        self.refresh_files()
        self.root.after(50, self.pump)

    # ---- thread bridge -------------------------------------------------
    def post(self, fn, *args) -> None:
        self.calls.put((fn, args, None))

    def call_main(self, fn, *args):
        result: queue.Queue = queue.Queue()
        self.calls.put((fn, args, result))
        value = result.get()
        if isinstance(value, Exception):
            raise value
        return value

    def pump(self) -> None:
        try:
            while True:
                fn, args, result = self.calls.get_nowait()
                try:
                    value = fn(*args)
                except Exception as exc:
                    value = exc
                if result is not None:
                    result.put(value)
        except queue.Empty:
            pass
        self.root.after(50, self.pump)

    # ---- layout --------------------------------------------------------
    def build(self) -> None:
        root = self.root
        root.title("GPT Agent")
        root.geometry("1320x820")
        root.minsize(1000, 640)
        root.configure(fg_color=BG)
        root.grid_columnconfigure(1, weight=1)
        root.grid_rowconfigure(0, weight=1)

        self.build_sidebar()

        main = ctk.CTkFrame(root, fg_color="transparent")
        main.grid(row=0, column=1, sticky="nsew", padx=(12, 6), pady=12)
        main.grid_rowconfigure(1, weight=1)
        main.grid_columnconfigure(0, weight=1)
        ctk.CTkLabel(main, text="Conversation", font=(FONT, 18, "bold"), text_color=TEXT).grid(
            row=0, column=0, sticky="w", padx=4, pady=(0, 8))
        self.chat = ctk.CTkScrollableFrame(main, fg_color=CARD, corner_radius=16)
        self.chat.grid(row=1, column=0, sticky="nsew")
        self.chat.bind("<Configure>", self.on_chat_resize)

        composer = ctk.CTkFrame(main, fg_color=CARD, corner_radius=16)
        composer.grid(row=2, column=0, sticky="ew", pady=(10, 0))
        composer.grid_columnconfigure(0, weight=1)
        self.prompt_label = ctk.CTkLabel(composer, text="New task", font=(FONT, 12, "bold"), text_color=ACCENT)
        self.prompt_label.grid(row=0, column=0, sticky="w", padx=16, pady=(10, 0))
        self.entry = ctk.CTkTextbox(composer, height=70, fg_color=CARD2, corner_radius=12,
                                    font=(FONT, 13), text_color=TEXT, wrap="word")
        self.entry.grid(row=1, column=0, sticky="ew", padx=(12, 8), pady=10)
        self.entry.bind("<Return>", self.on_enter)
        self.send_btn = ctk.CTkButton(composer, text="Send  ➤", width=100, height=70, corner_radius=12,
                                      fg_color=ACCENT, hover_color=ACCENT_HOVER,
                                      font=(FONT, 13, "bold"), command=self.submit)
        self.send_btn.grid(row=1, column=1, padx=(0, 12), pady=10)

        side = ctk.CTkFrame(root, fg_color="transparent")
        side.grid(row=0, column=2, sticky="nsew", padx=(6, 12), pady=12)
        side.grid_rowconfigure(1, weight=1)
        side.grid_columnconfigure(0, weight=1)
        ctk.CTkLabel(side, text="Activity", font=(FONT, 18, "bold"), text_color=TEXT).grid(
            row=0, column=0, sticky="w", padx=4, pady=(0, 8))
        tabs = ctk.CTkTabview(side, width=380, fg_color=CARD, corner_radius=16,
                              segmented_button_fg_color=CARD2, segmented_button_selected_color=ACCENT,
                              segmented_button_selected_hover_color=ACCENT_HOVER)
        tabs.grid(row=1, column=0, sticky="nsew")
        for name in ("Actions", "Changes", "Audit", "Files"):
            tabs.add(name)
            tabs.tab(name).grid_columnconfigure(0, weight=1)
            tabs.tab(name).grid_rowconfigure(0, weight=1)
        self.actions_list = self.make_list(tabs.tab("Actions"))
        self.changes_list = self.make_list(tabs.tab("Changes"))
        self.audit_list = self.make_list(tabs.tab("Audit"))
        self.files_list = self.make_list(tabs.tab("Files"))
        ctk.CTkButton(tabs.tab("Files"), text="Refresh", height=30, fg_color=CARD2, hover_color="#2f3547",
                      command=self.refresh_files).grid(row=1, column=0, sticky="ew", pady=(6, 0))
        self.detail = ctk.CTkTextbox(side, height=160, fg_color=CARD, corner_radius=16,
                                     font=("Consolas", 11), text_color=MUTED, wrap="word")
        self.detail.grid(row=2, column=0, sticky="ew", pady=(10, 0))
        self.detail.configure(state="disabled")

        bar = ctk.CTkFrame(root, fg_color=SIDEBAR, corner_radius=0, height=28)
        bar.grid(row=1, column=0, columnspan=3, sticky="ew")
        ctk.CTkLabel(bar, textvariable=self.status, font=(FONT, 11), text_color=MUTED,
                     anchor="w").pack(side="left", padx=14, pady=4)
        root.protocol("WM_DELETE_WINDOW", self.on_close)
        self.entry.focus_set()

    def build_sidebar(self) -> None:
        sb = ctk.CTkFrame(self.root, fg_color=SIDEBAR, corner_radius=0, width=230)
        sb.grid(row=0, column=0, sticky="nsw")
        sb.pack_propagate(False)
        ctk.CTkLabel(sb, text="🤖", font=("Segoe UI Emoji", 40)).pack(pady=(26, 0))
        ctk.CTkLabel(sb, text="GPT Agent", font=(FONT, 22, "bold"), text_color=TEXT).pack()
        ctk.CTkLabel(sb, text="ChatGPT-driven local agent", font=(FONT, 11), text_color=MUTED).pack(pady=(0, 24))

        ctk.CTkLabel(sb, text="BROWSER MODE", font=(FONT, 10, "bold"), text_color=MUTED).pack(anchor="w", padx=20)
        ctk.CTkSegmentedButton(sb, values=["Off-screen", "Visible", "Headless"], variable=self.mode,
                               fg_color=CARD2, selected_color=ACCENT, selected_hover_color=ACCENT_HOVER,
                               unselected_color=CARD2, font=(FONT, 10)).pack(fill="x", padx=12, pady=(6, 18))

        self.bypass_var = ctk.BooleanVar(value=False)
        self.bypass_text = ""
        ctk.CTkSwitch(sb, text="Context bypass", variable=self.bypass_var, progress_color=ACCENT,
                      font=(FONT, 12), text_color=TEXT, command=self.toggle_bypass).pack(anchor="w", padx=20, pady=(0, 14))

        self.signin_btn = self.side_button(sb, "🔑  Sign in to ChatGPT", lambda: self.start_session(account_only=True))
        self.side_button(sb, "📁  Open workspace", self.open_workspace)
        self.side_button(sb, "💾  Save transcript", self.save_transcript)
        self.side_button(sb, "🧹  Clear conversation", self.clear_chat)
        self.stop_btn = ctk.CTkButton(sb, text="⏹  Stop session", height=38, corner_radius=10, state="disabled",
                                      fg_color="#3a1d24", hover_color="#51252f", text_color=RED,
                                      command=self.stop)
        self.stop_btn.pack(fill="x", padx=16, pady=(14, 6))

        ctk.CTkOptionMenu(sb, values=["Dark", "Light", "System"], fg_color=CARD2, button_color=CARD2,
                          button_hover_color="#2f3547", command=ctk.set_appearance_mode
                          ).pack(side="bottom", fill="x", padx=16, pady=(0, 16))
        ctk.CTkLabel(sb, text="APPEARANCE", font=(FONT, 10, "bold"), text_color=MUTED).pack(
            side="bottom", anchor="w", padx=20, pady=(0, 4))

    def side_button(self, parent, text: str, command) -> ctk.CTkButton:
        btn = ctk.CTkButton(parent, text=text, anchor="w", height=38, corner_radius=10, fg_color="transparent",
                            hover_color=CARD2, text_color=TEXT, font=(FONT, 12), command=command)
        btn.pack(fill="x", padx=12, pady=2)
        return btn

    def make_list(self, parent) -> ctk.CTkScrollableFrame:
        frame = ctk.CTkScrollableFrame(parent, fg_color="transparent")
        frame.grid(row=0, column=0, sticky="nsew")
        return frame

    # ---- output handlers (main thread) --------------------------------
    def on_chat_resize(self, event) -> None:
        for label in self.bubble_labels:
            try:
                label.configure(wraplength=max(240, int(event.width * 0.72)))
            except Exception:
                pass

    def scroll_chat(self) -> None:
        self.chat.update_idletasks()
        self.chat._parent_canvas.yview_moveto(1.0)

    def add_bubble(self, kind: str, message: str) -> None:
        wrap = max(240, int(self.chat.winfo_width() * 0.72))
        if kind == "notice":
            label = ctk.CTkLabel(self.chat, text=message, font=(FONT, 11, "italic"), text_color=MUTED,
                                 wraplength=wrap, justify="center")
            label.pack(pady=3)
            self.bubble_labels.append(label)
            self.scroll_chat()
            return
        mine = kind == "user"
        row = ctk.CTkFrame(self.chat, fg_color="transparent")
        row.pack(fill="x", pady=5, padx=4)
        bubble = ctk.CTkFrame(row, corner_radius=16, fg_color=ACCENT if mine else CARD2)
        bubble.pack(side="right" if mine else "left")
        title = "You" if mine else "ChatGPT"
        ctk.CTkLabel(bubble, text=f"{title} · {datetime.now():%H:%M}", font=(FONT, 10, "bold"),
                     text_color="#d9d2ff" if mine else GREEN).pack(anchor="w", padx=14, pady=(8, 0))
        label = ctk.CTkLabel(bubble, text=message.strip(), font=(FONT, 13), text_color="#ffffff" if mine else TEXT,
                             wraplength=wrap, justify="left", anchor="w")
        label.pack(anchor="w", padx=14, pady=(2, 10))
        self.bubble_labels.append(label)
        self.scroll_chat()

    def set_detail(self, text: str) -> None:
        self.detail.configure(state="normal")
        self.detail.delete("1.0", "end")
        self.detail.insert("1.0", text)
        self.detail.configure(state="disabled")

    def make_row(self, parent, status: str, title: str, subtitle: str = "", on_click=None) -> None:
        row = ctk.CTkFrame(parent, fg_color=CARD2, corner_radius=10)
        row.pack(fill="x", pady=3)
        ctk.CTkLabel(row, text="●", text_color=STATUS_COLORS.get(status, MUTED), width=20).pack(side="left", padx=(10, 0))
        box = ctk.CTkFrame(row, fg_color="transparent")
        box.pack(side="left", fill="x", expand=True, padx=6, pady=6)
        ctk.CTkLabel(box, text=title, font=(FONT, 12, "bold"), text_color=TEXT, anchor="w").pack(fill="x")
        if subtitle:
            ctk.CTkLabel(box, text=subtitle, font=(FONT, 11), text_color=MUTED, anchor="w").pack(fill="x")
        if on_click:
            for widget in (row, box, *box.winfo_children()):
                widget.bind("<Button-1>", lambda _e: on_click())

    def add_action(self, name: str, arg: str, status: str, detail: str) -> None:
        shown = f"{name} {arg} [{status}]\n{(detail or '(no output)')[:3000]}"
        self.make_row(self.actions_list, status, f"{name}  {arg}".strip(),
                      f"{datetime.now():%H:%M:%S} · {status}", lambda: self.set_detail(shown))
        self.set_detail(shown)
        self.refresh_files()

    @staticmethod
    def clear_frame(frame) -> None:
        for child in frame.winfo_children():
            child.destroy()

    def show_changes(self, files: dict, added: int, removed: int) -> None:
        self.clear_frame(self.changes_list)
        for path, (before, after) in sorted(files.items()):
            a = r = 0
            old, new = (before or "").splitlines(), (after or "").splitlines()
            for tag, i1, i2, j1, j2 in SequenceMatcher(None, old, new).get_opcodes():
                if tag in ("replace", "delete"):
                    r += i2 - i1
                if tag in ("replace", "insert"):
                    a += j2 - j1
            self.make_row(self.changes_list, "ok", path, f"+{a}  -{r}")
        self.status.set(f"Files changed: {len(files)}   +{added}   -{removed}")

    def show_audit(self, entries: list[dict]) -> None:
        self.clear_frame(self.audit_list)
        for e in entries:
            self.make_row(self.audit_list, e["status"], f"{e['action']}  {e['arg']}".strip(),
                          f"{e['time']} · {e['status']}")

    def refresh_files(self) -> None:
        self.clear_frame(self.files_list)
        WORKSPACE.mkdir(exist_ok=True)
        for path in sorted(WORKSPACE.rglob("*"), key=lambda p: p.relative_to(WORKSPACE).parts):
            rel = path.relative_to(WORKSPACE)
            icon = "📂" if path.is_dir() else "📄"
            label = ctk.CTkLabel(self.files_list, text=f"{'    ' * (len(rel.parts) - 1)}{icon} {path.name}",
                                 anchor="w", font=(FONT, 12), text_color=TEXT)
            label.pack(fill="x", pady=1)
            if path.is_file():
                label.bind("<Double-Button-1>", lambda _e, p=path: os.startfile(p))

    def toggle_bypass(self) -> None:
        if not self.bypass_var.get():
            return
        win = ctk.CTkToplevel(self.root)
        win.title("Context bypass")
        win.configure(fg_color=BG)
        win.geometry("520x340")
        win.transient(self.root)
        ctk.CTkLabel(win, text="Context bypass text", font=(FONT, 16, "bold"), text_color=TEXT).pack(pady=(16, 2))
        ctk.CTkLabel(win, text="Sent to ChatGPT instead of the context-marker check, which is disabled.",
                     font=(FONT, 11), text_color=MUTED).pack()
        box = ctk.CTkTextbox(win, fg_color=CARD, font=(FONT, 12), wrap="word")
        box.pack(fill="both", expand=True, padx=18, pady=10)
        box.insert("1.0", self.bypass_text or "Lorem ipsum dolor sit amet, consectetur adipiscing elit.")
        row = ctk.CTkFrame(win, fg_color="transparent")
        row.pack(pady=(0, 14))

        def save() -> None:
            text = box.get("1.0", "end").strip()
            self.bypass_text = text
            if not text:
                self.bypass_var.set(False)
            win.destroy()

        def cancel() -> None:
            self.bypass_var.set(bool(self.bypass_text))
            win.destroy()

        ctk.CTkButton(row, text="Save", fg_color=ACCENT, hover_color=ACCENT_HOVER, command=save).pack(side="left", padx=8)
        ctk.CTkButton(row, text="Cancel", fg_color=CARD2, hover_color="#2f3547", command=cancel).pack(side="left", padx=8)
        win.protocol("WM_DELETE_WINDOW", cancel)
        win.after(150, lambda: (win.lift(), win.grab_set(), win.focus_force()))

    def dialog(self, title: str, message: str, ask: bool) -> bool:
        win = ctk.CTkToplevel(self.root)
        win.title(title)
        win.configure(fg_color=BG)
        win.geometry("480x280")
        win.transient(self.root)
        result = {"value": False}
        ctk.CTkLabel(win, text=title, font=(FONT, 16, "bold"), text_color=TEXT).pack(pady=(18, 6))
        box = ctk.CTkTextbox(win, fg_color=CARD, font=("Consolas", 12), wrap="word", height=120)
        box.pack(fill="both", expand=True, padx=18)
        box.insert("1.0", message)
        box.configure(state="disabled")
        row = ctk.CTkFrame(win, fg_color="transparent")
        row.pack(pady=14)

        def close(value: bool) -> None:
            result["value"] = value
            win.destroy()

        if ask:
            ctk.CTkButton(row, text="Allow", fg_color=GREEN, hover_color="#22b07d", text_color="#06281c",
                          command=lambda: close(True)).pack(side="left", padx=8)
            ctk.CTkButton(row, text="Refuse", fg_color="#3a1d24", hover_color="#51252f", text_color=RED,
                          command=lambda: close(False)).pack(side="left", padx=8)
        else:
            ctk.CTkButton(row, text="OK", fg_color=ACCENT, hover_color=ACCENT_HOVER,
                          command=lambda: close(True)).pack()
        win.protocol("WM_DELETE_WINDOW", lambda: close(False))
        win.after(150, lambda: (win.lift(), win.grab_set(), win.focus_force()))
        self.root.wait_window(win)
        return result["value"]

    # ---- input ---------------------------------------------------------
    def begin_input(self, label: str) -> None:
        self.waiting = True
        self.prompt_label.configure(text=label or "Your answer")
        self.entry.focus_set()
        self.status.set("Waiting for your input")

    def on_enter(self, event) -> str | None:
        if event.state & 0x1:  # Shift+Enter inserts a newline
            return None
        self.submit()
        return "break"

    def submit(self) -> None:
        text = self.entry.get("1.0", "end").strip()
        if self.waiting:
            self.waiting = False
            self.entry.delete("1.0", "end")
            self.prompt_label.configure(text="Working...")
            self.answer.put(text)
            return
        if (self.worker and self.worker.is_alive()) or not text:
            return
        self.entry.delete("1.0", "end")
        self.start_session(task=text)

    # ---- session -------------------------------------------------------
    def set_running(self, running: bool) -> None:
        self.stop_btn.configure(state="normal" if running else "disabled")
        self.signin_btn.configure(state="disabled" if running else "normal")
        if not running:
            self.waiting = False
            self.prompt_label.configure(text="New task")
            self.status.set("Ready")
            self.refresh_files()

    def start_session(self, task: str = "", account_only: bool = False) -> None:
        if self.worker and self.worker.is_alive():
            return
        self.stop_requested.clear()
        self.answer = queue.Queue()
        AUDIT.clear()
        self.ui.file_states.clear()
        mode = self.mode.get()
        agent.CONTEXT_BYPASS["enabled"] = bool(self.bypass_var.get() and self.bypass_text)
        agent.CONTEXT_BYPASS["text"] = self.bypass_text
        args = Namespace(task=[task], account=account_only, gui=True,
                         visible=mode == "Visible" or account_only,
                         headless=mode == "Headless" and not account_only)
        self.clear_frame(self.actions_list)
        self.set_running(True)
        self.prompt_label.configure(text="Working...")
        self.worker = threading.Thread(target=self.run_worker, args=(args, account_only), daemon=True)
        self.worker.start()

    def run_worker(self, args: Namespace, account_only: bool) -> None:
        try:
            if account_only:
                agent.sign_in(self.ui)
            else:
                agent.run_session(self.ui, args)
        except Exception as exc:
            self.ui.notice(f"Error: {exc}")
        finally:
            self.post(self.set_running, False)

    def stop(self) -> None:
        self.stop_requested.set()
        self.status.set("Stopping after the current step...")
        if self.waiting:
            self.waiting = False
            self.answer.put("")

    def open_workspace(self) -> None:
        WORKSPACE.mkdir(exist_ok=True)
        os.startfile(WORKSPACE)

    def save_transcript(self) -> None:
        path = filedialog.asksaveasfilename(defaultextension=".json", filetypes=[("JSON", "*.json")])
        if path:
            lines = [lbl.cget("text") for lbl in self.bubble_labels if lbl.winfo_exists()]
            with open(path, "w", encoding="utf-8") as f:
                json.dump({"messages": lines, "audit": AUDIT}, f, indent=2, ensure_ascii=False)

    def clear_chat(self) -> None:
        self.clear_frame(self.chat)
        self.bubble_labels.clear()

    def on_close(self) -> None:
        if self.worker and self.worker.is_alive():
            self.stop()
            deadline = time.time() + 3
            while self.worker.is_alive() and time.time() < deadline:
                self.root.update()
                time.sleep(0.05)
        self.root.destroy()


def splash(root: ctk.CTk, seconds: float = 6.0) -> None:
    win = ctk.CTkToplevel(root)
    win.overrideredirect(True)
    win.attributes("-alpha", 0.0)
    width, height = 520, 320
    x = (win.winfo_screenwidth() - width) // 2
    y = (win.winfo_screenheight() - height) // 2
    win.geometry(f"{width}x{height}+{x}+{y}")
    win.configure(fg_color=BG)
    card = ctk.CTkFrame(win, fg_color=SIDEBAR, corner_radius=0, border_width=2, border_color=ACCENT)
    card.pack(fill="both", expand=True)
    ctk.CTkLabel(card, text="🤖", font=("Segoe UI Emoji", 60)).pack(pady=(34, 0))
    ctk.CTkLabel(card, text="GPT Agent", font=(FONT, 32, "bold"), text_color=TEXT).pack()
    ctk.CTkLabel(card, text="ChatGPT-driven local agent", font=(FONT, 12), text_color=MUTED).pack()
    message = ctk.CTkLabel(card, text="Starting...", font=(FONT, 11), text_color=MUTED)
    message.pack(pady=(22, 6))
    bar = ctk.CTkProgressBar(card, width=340, height=8, progress_color=ACCENT, fg_color=CARD2)
    bar.pack()
    bar.set(0)
    steps = ["Loading modules...", "Preparing workspace...", "Connecting components...", "Building interface...", "Ready"]
    win.update()
    start = time.time()
    fade = 0.6
    while (elapsed := time.time() - start) < seconds:
        alpha = min(1.0, elapsed / fade, (seconds - elapsed) / fade)
        win.attributes("-alpha", max(0.0, alpha))
        bar.set(elapsed / seconds)
        message.configure(text=steps[min(int(elapsed / seconds * len(steps)), len(steps) - 1)])
        win.update()
        time.sleep(0.016)
    win.destroy()


def run_gui() -> None:
    ctk.set_appearance_mode("Dark")
    ctk.set_default_color_theme("dark-blue")
    root = ctk.CTk()
    root.withdraw()
    splash(root)
    WORKSPACE.mkdir(exist_ok=True)
    App(root)

    def show() -> None:
        root.deiconify()
        root.lift()
        root.focus_force()
        if root.state() == "withdrawn":  # CTk's delayed title-bar setup may hide it again
            root.after(200, show)

    root.after(400, show)
    root.mainloop()
