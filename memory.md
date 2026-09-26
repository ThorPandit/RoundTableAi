# Local Mind — Memory

## About this user
- Username: 106627
- Name: Shubham
- Machine: Windows 11 laptop with 2 monitors
- Prefers short, direct answers

## CRITICAL RULES (read every time)

### When to STOP
- The instant the requested action succeeds, call done(). Do NOT keep verifying.
- If write_file returns "OK: ... (verified)", the task is DONE. Do NOT read the file again.
- If read_file returns the expected content, the task is DONE. Do NOT read again.
- If list_dir returns a list, the task is DONE. Do NOT list again.
- It is BETTER to finish too early than to do extra unwanted actions.
- If unsure whether to continue, call done().

### Task patterns
- "Open X" = ONE open action, then done. Do NOT type, do NOT verify.
- "Open X and type Y" = TWO actions: open, then type. Do NOT stop after open.
- "Open X then type Y" = TWO actions.
- "Write X in Notepad" = use write_file to save, then open the file.
- "Create a file" = ONE write_file command. Then done.
- "Change the content of X to Y" = ONE write_file command (overwrites). Then done.

### Never do
- NEVER type the task description into an app.
- NEVER invent extra work.
- NEVER do the same action twice in a row.
- NEVER use shell echo for files — use write_file. It verifies automatically.

### When to ask
- If a task needs info you don't have (user's name, a file location, a preference),
  use clarify("your question"). Do NOT guess.
- Example: "type my name in notepad" → clarify("What is your name?")
  Then use the answer on the next turn.

## Screen reading (new)
- Prefer `list_windows()` and `list_controls(window)` over vision for finding buttons.
- Prefer `click_control(window, control)` over `click(x, y)` — it's deterministic.
- Use `describe_screen()` only when structured reading fails.
- Use `cmd_window(command)` when the user wants to SEE the command running.
- Use `shell(command)` when the user only needs the output.

## RAG
- The agent receives "SIMILAR PAST TASKS" before each task. Use this for context.
- If a past task worked, follow the same pattern.
- If a past task failed, avoid the same approach.

## Preferences
- Prefer `shell` over GUI clicks. More reliable.
- Full paths only: C:\Users\106627\Desktop\file.txt
- Desktop: C:\Users\106627\Desktop
- Documents: C:\Users\106627\Documents
## Auto-learned lessons
- [2026-09-25] The user's desktop directory is accessible and writable, allowing file creations at C:\Users\106627\Desktop\.
- [2026-09-25] When launching a GUI application from a script, use the 'shell' or 'launch_app' action instead of 'cmd_window' or 'start'.
