# GPT Agent

**A local, Windows-first coding agent that turns a ChatGPT conversation into reviewed file operations.**

GPT Agent connects to ChatGPT in Chrome with Selenium, reads its structured action directives, and applies them inside a dedicated `workspace` folder. A Rich-powered terminal interface shows the conversation, action results, and a live file-change summary.

> This project automates the ChatGPT website. It is not an OpenAI API client and is not affiliated with GitHub Copilot.

## What it does

- Opens ChatGPT in Chrome and reuses a local browser profile for your sign-in.
- Creates, appends, edits, reads, moves, and deletes workspace files.
- Runs approved shell commands from the workspace.
- Shows changed text files and added or removed lines during the session.
- Checks each response for a per-session context marker and resends the protocol if it is missing.
- Writes a JSON audit report after the session.

## Requirements

- Windows 10 or 11
- Python 3.10 or later
- Google Chrome
- A ChatGPT account and internet access

Selenium Manager normally obtains the matching ChromeDriver automatically. The first run may take longer while it initializes the browser.

## Quick start

Open PowerShell in the project folder:

```powershell
py -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install -r requirements.txt
python agent.py
```

Enter a task in the terminal. Sign in to ChatGPT in the Chrome window if needed, then return to the terminal and press Enter. The agent displays the conversation and asks before running commands or performing destructive actions.

Files created by the agent are placed in `workspace/`. After ChatGPT reports `.done`, you can enter another task in the same session. Enter `/quit` to close the app.

## Actions

| Directive | Operation |
| --- | --- |
| `.newfile` | Create a file; asks before overwriting an existing file. |
| `.appendfile` | Append content to a file. |
| `.editfile` | Replace one unique text block in a file. |
| `.readfile` | Read a workspace file. |
| `.listdir` | List workspace contents. |
| `.movefile` | Move a file or folder without overwriting an existing destination. |
| `.deletefile` | Delete a workspace file or folder. |
| `.run` | Run a shell command from a workspace directory. |
| `.ask` | Ask you a question in the terminal. |
| `.done` | Finish the current task and offer to start another. |

The `.context` directive is an internal response check and is not an operation.

## Safety and privacy

- File directives are restricted to `workspace/`; path traversal and absolute paths are rejected.
- The context marker checks that ChatGPT follows the expected response protocol. It cannot prove that the model retained every detail of the conversation.
- Review each `.run` command before approving it. Commands run as your Windows user; using `workspace/` as the working directory does **not** sandbox the command from the rest of your computer.
- The local `chrome-profile/` directory can contain your signed-in browser session. Never publish or share it.
- Audit files can contain task text, command output, and file paths. Keep them private.
- ChatGPT's website and page structure can change, which may require updates to the Selenium selectors.

The GitHub ZIP intentionally excludes the Chrome profile, virtual environment, audit logs, Python caches, and generated workspace files.

## Project layout

```text
agent.py          Selenium agent and terminal interface
requirements.txt  Python dependencies
workspace/        Local working folder for generated files
```

## License

No license is included yet. Add a license before redistributing the project if you want to grant reuse rights.