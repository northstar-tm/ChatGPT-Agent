"""Local agent driven by ChatGPT (web interface) through Selenium.

ChatGPT answers with directives in this format:

    .newfile = path/file.py
    ```
    content
    ```

The script reads the answer, runs the directives inside WORKSPACE, then sends
a report back to ChatGPT to continue the loop.
"""

import json
import re
import secrets
import shutil
import subprocess
import sys
import time
from collections import Counter
from datetime import datetime
from difflib import SequenceMatcher
from pathlib import Path

from rich import box
from rich.console import Console
from rich.markdown import Markdown
from rich.panel import Panel
from rich.rule import Rule
from rich.table import Table
from rich.text import Text
from selenium import webdriver
from selenium.common.exceptions import TimeoutException
from selenium.webdriver.common.by import By
from selenium.webdriver.common.keys import Keys
from selenium.webdriver.support.ui import WebDriverWait

CHATGPT_URL = "https://chatgpt.com/"
WORKSPACE = Path(__file__).parent / "workspace"
PROFILE_DIR = Path(__file__).parent / "chrome-profile"  # keeps the login session
RESPONSE_TIMEOUT = 300
STABLE_SECONDS = 3

SYSTEM_PROMPT = """You are an agent that controls my computer through a script. \
The OS is Windows; the working directory is a sandbox folder. \
To act, reply ONLY with directives, each on its own line followed by a \
markdown code block:

.newfile = relative/path/file.ext
```
full file content
```

.appendfile = relative/path/file.ext
```
content to append
```

.run = relative directory (or .)
```
shell command
```

.editfile = relative/path/file.ext
```
<<<<<<< SEARCH
exact text to replace (must be unique)
=======
new text
>>>>>>> REPLACE
```

.deletefile = relative/path (file or folder)
```
-
```

.movefile = source -> destination
```
-
```

.readfile = relative/path/file.ext
```
-
```

.listdir = relative/path (or .)
```
-
```

.ask =
```
question for the user
```

.done =
```
final message
```

Rules: relative paths only, one directive per block (put - when there is no \
content), no text outside directives, never leave a code block empty. After \
each execution I send you the result; the user may refuse an action or send \
you a message. Send .done only after the requested work is complete and \
successful."""

ALLOWED_DIRECTIVES = {
    ".context", ".newfile", ".appendfile", ".run", ".editfile", ".deletefile",
    ".movefile", ".readfile", ".listdir", ".ask", ".done",
}

DESTRUCTIVE = {".deletefile", ".movefile", ".editfile", ".run"}
MAX_READ = 20000
AUDIT: list[dict] = []

# Response blocks (p/pre) of the page; ChatGPT does not always expose one container per message.
JS_BLOCKS = """
const getB = () => [...document.querySelectorAll(
  '.markdown p, .markdown pre, p[data-assistant-stream-block], pre'
)].filter(x => !x.parentElement.closest('pre'));
"""

# Extracts [(directive, content)] from the blocks added after index arguments[0].
JS_PARSE = JS_BLOCKS + """
const out = [];
let pending = null;
for (const el of getB().slice(arguments[0])) {
  if (el.tagName === 'PRE') {
    const code = el.querySelector('code');
    out.push([pending, (code || el).textContent]);
    pending = null;
  } else {
    pending = el.textContent.trim();
  }
}
return out;
"""


def make_driver() -> webdriver.Chrome:
    opts = webdriver.ChromeOptions()
    opts.add_argument(f"--user-data-dir={PROFILE_DIR}")
    opts.add_argument("--start-maximized")
    return webdriver.Chrome(options=opts)


ASSISTANT_SEL = '[data-message-author-role="assistant"]'
USER_SEL = '[data-message-author-role="user"]'
COMPOSER_SEL = "#prompt-textarea, div.ProseMirror[contenteditable='true'], textarea"
SEND_BUTTON_SEL = (
    'button[data-testid="send-button"], button[aria-label*="Send"], button[aria-label*="Envoyer"]'
)


def log(msg: str) -> None:
    print(f"[{datetime.now():%H:%M:%S}] {msg}", flush=True)


def read_text_or_none(path: Path) -> str | None:
    try:
        return path.read_text(encoding="utf-8") if path.is_file() else None
    except (OSError, UnicodeError):
        return None


def snapshot_text_files(path: Path) -> dict[Path, str]:
    paths = [path] if path.is_file() else path.rglob("*") if path.is_dir() else []
    snapshot = {}
    for file_path in paths:
        if file_path.is_file():
            text = read_text_or_none(file_path)
            if text is not None:
                snapshot[file_path] = text
    return snapshot


class ChatUI:
    def __init__(self) -> None:
        self.console = Console()
        self.file_states: dict[str, tuple[str | None, str | None]] = {}

    def start(self) -> None:
        self.console.print(Panel(
            "ChatGPT agent connected through Chrome",
            title="[bold cyan]GPT Agent[/]",
            border_style="cyan",
            box=box.ROUNDED,
            expand=False,
        ))

    def prompt(self, label: str) -> str:
        return self.console.input(f"[bold cyan]You[/] [dim]>[/] {label} ")

    def user(self, message: str) -> None:
        self.console.print(Panel(Text(message, overflow="fold"), title="You", border_style="cyan", box=box.ROUNDED))

    def assistant(self, message: str) -> None:
        self.console.print(Panel(Markdown(message), title="ChatGPT", border_style="green", box=box.ROUNDED))

    def notice(self, message: str) -> None:
        self.console.print(f"[dim]{message}[/]")

    def action(self, name: str, arg: str, status: str, detail: str) -> None:
        style = "green" if status == "ok" else "yellow" if status == "refused" else "red"
        self.console.print(f"[{style}]{status.upper()}[/] [bold]{name}[/] {arg}")
        if detail:
            self.console.print(Text(detail[:500], style="dim", overflow="fold"))

    def track_file(self, path: Path, before: str | None, after: str | None) -> None:
        key = path.relative_to(WORKSPACE.resolve()).as_posix()
        original = self.file_states.get(key, (before, before))[0]
        if original == after:
            self.file_states.pop(key, None)
        else:
            self.file_states[key] = (original, after)

    def track_snapshots(self, before: dict[Path, str], after: dict[Path, str]) -> None:
        for path in before.keys() | after.keys():
            self.track_file(path, before.get(path), after.get(path))

    def change_totals(self) -> tuple[int, int]:
        added = removed = 0
        for before, after in self.file_states.values():
            old_lines = (before or "").splitlines()
            new_lines = (after or "").splitlines()
            for tag, i1, i2, j1, j2 in SequenceMatcher(None, old_lines, new_lines).get_opcodes():
                if tag in ("replace", "delete"):
                    removed += i2 - i1
                if tag in ("replace", "insert"):
                    added += j2 - j1
        return added, removed

    def change_bar(self) -> None:
        added, removed = self.change_totals()
        count = len(self.file_states)
        self.console.print(Rule(
            f"[bold]Files changed: {count}[/bold]  [green]+{added}[/green]  [red]-{removed}[/red]  [dim]ChatGPT via Chrome[/dim]",
            style="bright_black",
        ))
        if self.file_states:
            for path, (before, after) in sorted(self.file_states.items()):
                file_added = file_removed = 0
                old_lines = (before or "").splitlines()
                new_lines = (after or "").splitlines()
                for tag, i1, i2, j1, j2 in SequenceMatcher(None, old_lines, new_lines).get_opcodes():
                    if tag in ("replace", "delete"):
                        file_removed += i2 - i1
                    if tag in ("replace", "insert"):
                        file_added += j2 - j1
                self.console.print(f"  [dim]{path}[/] [green]+{file_added}[/] [red]-{file_removed}[/]")

    def audit(self, entries: list[dict]) -> None:
        table = Table(title="Run summary", box=box.SIMPLE)
        table.add_column("#", style="dim", justify="right")
        table.add_column("Time", style="dim")
        table.add_column("Status")
        table.add_column("Action")
        table.add_column("Target")
        for index, entry in enumerate(entries, 1):
            status_style = "green" if entry["status"] == "ok" else "yellow" if entry["status"] == "refused" else "red"
            table.add_row(str(index), entry["time"], f"[{status_style}]{entry['status']}[/]", entry["action"], entry["arg"])
        self.console.print(table)


def assistant_count(driver) -> int:
    return driver.execute_script(JS_BLOCKS + "return getB().length;")


def user_count(driver) -> int:
    return len(driver.find_elements(By.CSS_SELECTOR, USER_SEL))


def find_composer(driver):
    def visible_box(d):
        for el in d.find_elements(By.CSS_SELECTOR, COMPOSER_SEL):
            if el.is_displayed():
                return el
        return False

    return WebDriverWait(driver, 60).until(visible_box)


def composer_text(driver, box) -> str:
    return driver.execute_script("return arguments[0].value ?? arguments[0].innerText ?? '';", box).strip()


def send_message(driver, text: str, attempts: int = 3) -> None:
    """Types the text, sends it and checks that the page reacts."""
    before_user, before_assistant = user_count(driver), assistant_count(driver)
    for attempt in range(1, attempts + 1):
        log(f"Sending (attempt {attempt}/{attempts})...")
        box = find_composer(driver)
        driver.execute_script("arguments[0].scrollIntoView({block:'center'}); arguments[0].focus();", box)
        if not composer_text(driver, box):
            # insertText keeps line breaks without triggering the send
            driver.execute_script("document.execCommand('insertText', false, arguments[0]);", text)
            time.sleep(0.5)
        if not composer_text(driver, box):
            box.send_keys(text.replace("\n", " "))  # fallback if execCommand is ignored

        try:
            button = WebDriverWait(driver, 10).until(
                lambda d: next(
                    (b for b in d.find_elements(By.CSS_SELECTOR, SEND_BUTTON_SEL)
                     if b.is_displayed() and b.is_enabled()),
                    False,
                )
            )
            driver.execute_script("arguments[0].click();", button)
        except TimeoutException:
            box.send_keys(Keys.ENTER)

        try:
            WebDriverWait(driver, 15).until(lambda d: is_sent(d, box, before_user, before_assistant))
            log("Message sent.")
            return
        except TimeoutException:
            log("Send not confirmed.")
    raise TimeoutException("Could not send the message to ChatGPT")


def is_sent(driver, box, before_user: int, before_assistant: int) -> bool:
    try:
        emptied = not composer_text(driver, box)
    except Exception:  # composer replaced in the DOM after sending
        emptied = True
    return (
        emptied
        or user_count(driver) > before_user
        or assistant_count(driver) > before_assistant
        or bool(driver.find_elements(By.CSS_SELECTOR, 'button[data-testid="stop-button"]'))
    )


def last_text(driver, since: int) -> str:
    return driver.execute_script(
        JS_BLOCKS + "return getB().slice(arguments[0]).map(e => e.innerText).join('\\n');", since
    )


def wait_for_response(driver, previous_count: int) -> str:
    deadline = time.time() + RESPONSE_TIMEOUT
    while assistant_count(driver) <= previous_count:
        if time.time() > deadline:
            raise TimeoutException("No response from ChatGPT")
        time.sleep(0.5)
    log("Response is being generated...")
    # Done = text unchanged for STABLE_SECONDS and no "stop" button
    text, last_change = last_text(driver, previous_count), time.time()
    while time.time() < deadline:
        time.sleep(0.5)
        current = last_text(driver, previous_count)
        if current != text:
            text, last_change = current, time.time()
            continue
        generating = driver.find_elements(By.CSS_SELECTOR, 'button[data-testid="stop-button"]')
        if text and not generating and time.time() - last_change >= STABLE_SECONDS:
            return text
    raise TimeoutException("The response never finishes")


def parse_response(driver, since: int) -> list[tuple[str, str, str]]:
    blocks = driver.execute_script(JS_PARSE, since) or []
    actions = []
    for header, content in blocks:
        if not header or not header.startswith("."):
            continue
        name, _, arg = header.partition("=")
        actions.append((name.strip().lower(), arg.strip(), content.rstrip("\n") + "\n"))
    return actions


def validate_response(actions: list[tuple[str, str, str]], canary: str) -> tuple[str | None, list[tuple[str, str, str]]]:
    markers = [action for action in actions if action[0] == ".context"]
    if len(markers) != 1 or markers[0][1] != canary or markers[0][2].strip() != canary:
        return "The required session context marker is missing or incorrect.", []
    unknown = [name for name, _, _ in actions if name not in ALLOWED_DIRECTIVES]
    if unknown:
        return "Unknown directive(s): " + ", ".join(unknown), []
    usable = [action for action in actions if action[0] != ".context"]
    if not usable:
        return "No actionable directive was found.", []
    return None, usable


def context_protocol(canary: str) -> str:
    return (
        "\n\nFor every reply, begin with this required context-check directive, "
        "including its code block exactly:\n\n"
        f".context = {canary}\n```\n{canary}\n```\n\n"
        "This marker is internal validation only; do not treat it as an action."
    )


def protocol_reminder(canary: str, task: str, issue: str) -> str:
    return (
        "Protocol reminder: the previous response was not executed because " + issue + " "
        "Re-read and follow the full directive method below. Put each directive "
        "on its own line and follow it with a Markdown code block. Use .newfile "
        "for creating a file and .done only when the requested work is complete. "
        "Do not claim that an action succeeded unless its result was reported.\n\n"
        + SYSTEM_PROMPT + context_protocol(canary) + "\n\nTask in progress: " + task
    )


def safe_path(relative: str) -> Path:
    if not relative.strip():
        raise ValueError("A relative path is required")
    if Path(relative).is_absolute():
        raise ValueError("Absolute paths are not allowed")
    path = (WORKSPACE / relative).resolve()
    if WORKSPACE.resolve() not in path.parents and path != WORKSPACE.resolve():
        raise ValueError(f"Path outside the workspace refused: {relative}")
    return path


def record(action: str, arg: str, status: str, detail: str = "") -> None:
    AUDIT.append({
        "time": datetime.now().strftime("%H:%M:%S"),
        "action": action,
        "arg": arg,
        "status": status,
        "detail": detail[:300],
    })


def confirm(question: str, ui: ChatUI | None = None) -> bool:
    prompt = f"[yellow]{question}[/] [y/N] " if ui else f"{question} [y/N] "
    answer = ui.console.input(prompt) if ui else input(prompt)
    return answer.strip().lower() in ("y", "yes")


def apply_edit(path: Path, content: str) -> str:
    match = re.search(
        r"<<<<<<< SEARCH\n(.*?)\n=======\n(.*?)>>>>>>> REPLACE", content, re.S
    )
    if not match:
        raise ValueError("Invalid SEARCH/REPLACE format")
    search, replace = match.group(1), match.group(2).removesuffix("\n")
    text = path.read_text(encoding="utf-8")
    if text.count(search) != 1:
        raise ValueError(f"The text to replace appears {text.count(search)} times (1 expected)")
    path.write_text(text.replace(search, replace), encoding="utf-8")
    return "edited"


def execute(action: str, arg: str, content: str, ui: ChatUI | None = None) -> tuple[str, str]:
    """Returns (status, message); status: ok | refused | error."""
    try:
        if action == ".ask":
            if ui:
                ui.assistant(content)
                return "ok", "User answer: " + ui.prompt("")
            print(f"\nChatGPT asks: {content}")
            return "ok", "User answer: " + input("> ")

        if action in (".newfile", ".appendfile", ".editfile") and not content.strip():
            return "error", f"{action} needs a non-empty code block"
        if action == ".run" and content.strip() in ("", "-"):
            return "error", ".run needs a shell command"

        if action in DESTRUCTIVE:
            shown = content if action == ".run" else arg
            if not confirm(f"\nChatGPT wants {action} {shown}\nAllow?", ui):
                return "refused", "Refused by the user"

        if action in (".newfile", ".appendfile"):
            path = safe_path(arg)
            if path.exists() and not path.is_file():
                raise ValueError(f"File path points to a directory: {path}")
            if action == ".newfile" and path.exists() and not confirm(f"{arg} already exists. Overwrite?", ui):
                return "refused", "Overwrite refused"
            path.parent.mkdir(parents=True, exist_ok=True)
            with path.open("w" if action == ".newfile" else "a", encoding="utf-8") as f:
                f.write(content)
            return "ok", f"{len(content)} characters written to {path}"

        if action == ".editfile":
            return "ok", apply_edit(safe_path(arg), content)

        if action == ".deletefile":
            path = safe_path(arg)
            if path == WORKSPACE.resolve():
                raise ValueError("Deleting the workspace is refused")
            if not path.exists():
                raise FileNotFoundError(f"Path does not exist: {path}")
            shutil.rmtree(path) if path.is_dir() else path.unlink()
            return "ok", "deleted"

        if action == ".movefile":
            if "->" not in arg:
                raise ValueError("Use .movefile = source -> destination")
            src, _, dst = arg.partition("->")
            src_path, dst_path = safe_path(src.strip()), safe_path(dst.strip())
            if not src_path.exists():
                raise FileNotFoundError(f"Source does not exist: {src_path}")
            if src_path == dst_path:
                raise ValueError("Source and destination are the same path")
            if src_path.is_dir() and src_path in dst_path.parents:
                raise ValueError("A directory cannot be moved inside itself")
            if dst_path.exists():
                raise FileExistsError(f"Destination already exists: {dst_path}")
            dst_path.parent.mkdir(parents=True, exist_ok=True)
            shutil.move(src_path, dst_path)
            return "ok", "moved"

        if action == ".readfile":
            return "ok", safe_path(arg).read_text(encoding="utf-8")[:MAX_READ]

        if action == ".listdir":
            base = safe_path(arg or ".")
            if not base.is_dir():
                raise NotADirectoryError(f"Not a directory: {base}")
            return "ok", "\n".join(
                p.relative_to(WORKSPACE.resolve()).as_posix() + ("/" if p.is_dir() else "")
                for p in sorted(base.rglob("*"))
            )

        if action == ".run":
            working_dir = safe_path(arg or ".")
            if not working_dir.is_dir():
                raise NotADirectoryError(f"Not a directory: {working_dir}")
            try:
                proc = subprocess.run(
                    content, shell=True, cwd=working_dir,
                    capture_output=True, text=True, timeout=120,
                )
            except subprocess.TimeoutExpired as exc:
                stdout = exc.stdout.decode(errors="replace") if isinstance(exc.stdout, bytes) else (exc.stdout or "")
                stderr = exc.stderr.decode(errors="replace") if isinstance(exc.stderr, bytes) else (exc.stderr or "")
                return "error", f"Command timed out after 120 seconds\nstdout:\n{stdout}\nstderr:\n{stderr}"
            status = "ok" if proc.returncode == 0 else "error"
            return status, f"code={proc.returncode}\nstdout:\n{proc.stdout}\nstderr:\n{proc.stderr}"

        return "error", f"Unknown directive: {action}"
    except Exception as exc:  # sent back to ChatGPT so it can fix the problem
        return "error", str(exc)


def show_audit(task: str, ui: ChatUI | None = None, tasks: list[str] | None = None) -> None:
    if ui:
        ui.audit(AUDIT)
        ui.change_bar()
    else:
        print("\n=== AUDIT ===")
        for i, e in enumerate(AUDIT, 1):
            print(f"{i:>3} {e['time']} {e['status']:<7} {e['action']} {e['arg']}")
    counts = Counter(e["status"] for e in AUDIT)
    if not ui:
        print("Total: " + ", ".join(f"{n} {s}" for s, n in counts.items()) if AUDIT else "No action.")
    path = WORKSPACE.parent / f"audit-{datetime.now():%Y%m%d-%H%M%S}.json"
    audit_data = {"task": task, "entries": AUDIT}
    if tasks and len(tasks) > 1:
        audit_data["tasks"] = tasks
    path.write_text(
        json.dumps(audit_data, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    if ui:
        ui.notice(f"Audit saved: {path}")
    else:
        print(f"Audit saved: {path}")


def main() -> None:
    WORKSPACE.mkdir(exist_ok=True)
    ui = ChatUI()
    ui.start()
    task = " ".join(sys.argv[1:]) or ui.prompt("What should we work on?")
    tasks = [task]
    canary = secrets.token_hex(8)
    ui.user(task)

    driver = make_driver()
    try:
        driver.get(CHATGPT_URL)
        ui.notice("Log in to ChatGPT in Chrome if needed, then press Enter here.")
        ui.console.input("[dim]Press Enter to continue...[/]")

        count = assistant_count(driver)
        ui.notice("Connecting to ChatGPT...")
        send_message(driver, SYSTEM_PROMPT + context_protocol(canary) + "\n\nTask: " + task)

        protocol_failures = 0
        while True:
            previous = count
            reply = wait_for_response(driver, previous)
            ui.assistant(reply)
            count = assistant_count(driver)
            actions = parse_response(driver, previous)
            issue, actions = validate_response(actions, canary)
            if issue:
                protocol_failures += 1
                record("protocol reminder", "", "error", issue)
                ui.notice(f"Protocol check failed ({protocol_failures}/3): {issue}")
                guidance = ""
                if protocol_failures >= 3:
                    guidance = ui.prompt("Still missing the protocol marker. Enter to remind again, type guidance, or /quit:").strip()
                    if guidance.lower() == "/quit":
                        record("quit", "", "ok", "Stopped after repeated protocol failures")
                        break
                    protocol_failures = 0
                reminder = protocol_reminder(canary, task, issue)
                if guidance:
                    reminder += "\n\nUser guidance: " + guidance
                ui.notice("Resending the directive method; no actions from that reply were executed.")
                send_message(driver, reminder)
                continue
            protocol_failures = 0

            report, finished = [], False
            for name, arg, content in actions:
                if name == ".done":
                    ui.notice("Task completed.")
                    record(name, "", "ok", content)
                    finished = True
                    break
                targets = []
                before = {}
                try:
                    if name in (".newfile", ".appendfile", ".editfile", ".deletefile") and arg:
                        targets = [safe_path(arg)]
                    elif name == ".movefile" and "->" in arg:
                        source, _, destination = arg.partition("->")
                        targets = [safe_path(source.strip()), safe_path(destination.strip())]
                    elif name == ".run":
                        targets = [WORKSPACE.resolve()]
                    for target in targets:
                        before.update(snapshot_text_files(target))
                except (OSError, ValueError):
                    targets, before = [], {}
                status, message = execute(name, arg, content, ui)
                after = {}
                for target in targets:
                    after.update(snapshot_text_files(target))
                ui.track_snapshots(before, after)
                ui.action(name, arg, status, message)
                ui.change_bar()
                record(name, arg, status, message)
                report.append(f"{name} {arg}: [{status}] {message}")
            if finished:
                close_app = False
                while True:
                    next_task = ui.prompt("Task complete. Enter another task, or /quit to exit:").strip()
                    if next_task.lower() == "/quit":
                        record("quit", "", "ok", "Application closed by the user")
                        close_app = True
                        break
                    if next_task:
                        task = next_task
                        tasks.append(task)
                        ui.user(task)
                        send_message(driver, "Task: " + task)
                        break
                if close_app:
                    break
                continue

            extra = ui.prompt("Message for ChatGPT (Enter = continue, 'stop' = quit):").strip()
            if extra.lower() == "stop":
                record("stop", "", "ok", "Stopped by the user")
                break
            if extra:
                ui.user(extra)
                record("user message", "", "ok", extra)
                report.append("User message: " + extra)

            ui.notice("Sending action results to ChatGPT...")
            send_message(
                driver,
                "Action results:\n" + "\n".join(report)
                + "\n\nContinue using the directive format. If the task is complete, send .done now; otherwise send the next required directive.",
            )
    except Exception:
        import traceback
        log("ERROR:\n" + traceback.format_exc())
    finally:
        show_audit(tasks[0], ui, tasks)
        try:
            ui.console.input("[dim]Press Enter to close Chrome...[/]")
        finally:
            driver.quit()


if __name__ == "__main__":
    main()
