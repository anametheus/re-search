# Re-Search

Local full-text search over every conversation in your claude.ai account —
inside projects and outside them — built from the account's data export.
One Python file, no dependencies, nothing leaves your PC.

Re-Search is an independent tool that reads the data export from Claude
(claude.ai). It is not affiliated with, endorsed by, or supported by Anthropic.
"Claude" is a trademark of Anthropic, PBC.

## Quick start

1. **Get the app.** Windows: download `Re-Search.exe` from the Releases
   page and double-click it (Windows SmartScreen will warn once because it's
   unsigned — "More info → Run anyway"). Mac, Linux, or if you'd rather run
   the source: install Python from python.org, then
   `python -m pip install pywebview` and `python re_search.py`.
2. **Export your data** from claude.ai: Settings → Privacy → Export data. You
   get a small `manifest-….json` file.
3. **Drop the manifest** on the app's Imports tab. It downloads your
   conversations, projects and memories and indexes them. Done — search.

Everything stays on your computer: the app talks to no server except
claude.ai's own download links for your export, and (only if you set up the
optional Ask tab with your own key) the Claude API. Your friends' data never
touches your machine and vice versa: each person runs their own export.

## Latest changes

**28 Sep 2026 — renamed to Re-Search**
- Changed: the app, exe, window, icon and data folder are now called
  Re-Search (files: `re_search.py`, `re-search.ico`, `Re-Search.exe`). On
  first run the old `ClaudeSearch` data folder is moved across, so nothing
  needs re-importing.
- Added: trademark disclaimer; the name avoids using "Claude" as part of the
  product name.

**28 Sep 2026 — own window**
- Added: with `pywebview` installed the app runs in its own native window
  (WebView2 inside) and closing the window quits the app. Without it, falls
  back to a chromeless Edge/Chrome app window, then to a browser tab.
- Added: `re-search.ico` (app and window icon), a "Quit Re-Search"
  button under Settings, `--browser` flag, and `log.txt` in the data folder
  for the console-less build.
- Changed: the build command now includes `--windowed`, `--icon` and
  `--collect-all webview`; the server runs in a background thread so the
  window can own the main one.

**28 Sep 2026 — click-to-jump and full match lists**
- Added: clicking a message match opens the chat scrolled to that exact
  message, with the term centred and the message outlined briefly.
- Added: "+N more" on a chat's card now shows the true number of matches in
  that chat and expands to list all of them in conversation order; "show
  fewer" collapses it.
- Changed: the results list uses one click handler for the whole list
  (checkboxes, matches, "+N more" and title rows), which also makes it faster
  on long result lists.

**28 Sep 2026 — bulk assignment and project files**
- Added: checkboxes on chats in the results list, a "select all shown" link,
  and an assign bar to put many chats into a project at once.
- Added: documents uploaded to projects are indexed and searchable, shown with
  a "file" badge and their project; new "Project files" option in the
  who-said-it filter. They stay out of the recent list and project counts.
- Changed: the assign endpoint accepts many chats in one call; memory notes
  and project files can't be reassigned.

**28 Sep 2026 — summaries and manual project assignment**
- Found: the export does not record which project a chat belongs to (a chat
  entry has only uuid, name, summary, dates, account and messages), so project
  counts start at 0 and the link has to be set by hand.
- Added: "in project" selector in the chat header; assignments are stored
  separately from imported data and survive re-imports.
- Added: each chat's summary from the export is indexed with the title and
  shown under the title in results and at the top of the chat view.
- Changed: an index built by an earlier version upgrades itself and re-reads
  the imports once on first run.

**28 Sep 2026 — Memories tab**
- Changed: memory notes moved out of chat search and the project dropdown
  into a Memories tab (general summary and files, then one section per
  project), with its own filter box. The "Memory notes" filter option was
  dropped. The Ask feature still draws on memory notes.

**27 Sep 2026 — memories and metadata zips**
- Added: the memories zip is indexed (general summary, per-project summaries,
  memory files).
- Dropped: the light_metadata zip (account record and login history) is
  recognised and deliberately skipped with a clear message.

**27 Sep 2026 — manifest import**
- Added: drop the export manifest on the Imports tab (or into the imports
  folder) and the app downloads the conversations, projects and memories zips
  itself, then imports them. Files already downloaded are never re-fetched,
  since each link works once. Clear messages when a link needs sign-in or has
  expired.
- Added: an import log in the Imports tab.
- Changed: `--import` copies the file into the imports folder and scans, so it
  works with a manifest too.

**27 Sep 2026 — new export layout**
- Changed: files inside the export zips are identified by content, not by
  name, so the new multi-zip export (with one JSON per project) imports
  correctly. Failed imports are remembered so the folder watcher doesn't retry
  them every 15 seconds.

**27 Sep 2026 — first version**
- Single-file Python app, no dependencies: SQLite full-text index, local web
  UI, imports folder watcher, phrase / OR / NOT / prefix search, filters by
  project, speaker and date, accent- and case-insensitive matching in Greek
  and Latin, snippets from the original text, optional Ask tab via the Claude
  API, one-line PyInstaller build.

## Contents of this repository

- `re_search.py` — the whole app
- `re-search.ico` — its icon
- `build.cmd` — double-click on Windows to build `Re-Search.exe`
- `README.md`, `LICENSE` (MIT), `.gitignore`

## 1. Get your data out of claude.ai

In claude.ai: **Settings → Privacy → Export data**. The download you get is a
small `manifest-….json`. Drop that file on the app's Imports tab (or into the
imports folder): the app downloads the conversations and projects zips it
lists and imports them in one go. Large accounts can take a few minutes.

Each download link in the manifest works exactly once. If the app reports that
a link needs sign-in, open that `export_url` in your browser instead, save the
zip, and drop the zip on the app. If it reports the link is used or expired,
request a new export.

Of the four zips in an export, three are used: conversations and projects
feed the search; memories (the notes Claude keeps about you) go to the
**Memories** tab, where you can browse the general memory and each project's
memory with a filter box of their own. They stay out of chat search results,
though the Ask feature does draw on them. The light_metadata zip holds only
your account record and login history and is deliberately not indexed.

Repeat whenever you want newer chats indexed; re-importing merges, it never
duplicates or deletes.

## 2. Run it

Requires Python 3.9+ from python.org (tick "Add to PATH" in the installer).
For the app to open in its own window rather than a browser tab, also run once:

```
python -m pip install pywebview
```

Then:

```
python re_search.py
```

With pywebview installed the interface opens in a Re-Search window (a
native window with the same interface inside; closing it quits the app).
Without it, the app opens as a chromeless Edge or Chrome "app window", and if
neither is found, in a normal browser tab. `--browser` forces a browser tab;
`--no-browser` just starts the server at `http://127.0.0.1:8765`.

Go to the **Imports** tab and drop the export manifest on it (see step 1).

## 3. Build a Windows .exe (optional)

Double-click `build.cmd` (it must sit next to `re_search.py` and
`re-search.ico`). It says which Python it is using, installs PyInstaller
and pywebview if needed and runs the build. If you prefer the Terminal, the
command it runs is this, typed as one line:

```
python -m PyInstaller --onefile --windowed --icon re-search.ico --collect-all webview --name Re-Search re_search.py
```

The result is `dist\Re-Search.exe`: its own icon, its own window, no console.
Windows SmartScreen may warn the first time because the exe is unsigned —
choose "More info → Run anyway". Pin it to Start if you like.

Because there is no console, anything the app would have printed goes to
`log.txt` in the data folder (see "Where things live"). If the built exe does
nothing when double-clicked, look there first; building without `--windowed`
shows the same messages in a console window instead.

If launching the exe gives an error like "The ordinal 380 could not be located
in the dynamic link library Re-Search.exe", the build itself is broken
(usually an interrupted or half-pasted build command). Delete the `build` and
`dist` folders and `Re-Search.spec`, and build again.

## Troubleshooting

- **`pyinstaller` (or another tool) "is not recognized"** — pip puts commands
  in a Scripts folder that isn't on PATH. Run tools through Python instead:
  `python -m PyInstaller …`, `python -m pip …`. Every command in this README
  is written that way.
- **The exe says "The ordinal 380 could not be located in the dynamic link
  library Re-Search.exe"** — the build is broken (typically interrupted).
  Delete `build`, `dist` and `Re-Search.spec`, then build again.
- **Windows SmartScreen blocks the exe** — it's unsigned. "More info → Run
  anyway", once.
- **The exe seems to do nothing** — there's no console; read `log.txt` in
  `%LOCALAPPDATA%\Re-Search`.
- **A manifest link "requires being signed in" or "has already been used"** —
  open that `export_url` in your browser and drop the zip on the app, or
  request a fresh export. Each link works exactly once.

## Searching

- plain words: all must appear (`budget spreadsheet`)
- exact phrase: `"quarterly report"`
- either: `python OR javascript` · exclude: `recipe NOT dessert`
- prefix: `photo*` (matches photo, photos, photography)
- accents and case are ignored, in Greek as well as Latin (`cafe` finds `Café`,
  `ελληνικα` finds `Ελληνικά`)
- filters: project (including "No project"), who said it (Me / Claude /
  Project files), date range; an empty search shows the most recent
  conversations

## Reading results

Results come in two groups: **Title matches** (the search term is in a chat's
title or summary) and **Message matches** (it's in the messages). Each message
match shows a few lines of context with the term highlighted; the number of
matches per chat is the real total, not just what's shown.

- click a match to open the chat on the right, scrolled to that exact message,
  with the term centred and the message outlined for a moment
- click **+N more** at the bottom of a chat's card to list every match in that
  chat, in the order they occur; **show fewer** collapses it again
- click the chat's title row to open it at its first match
- in the chat view, matches are highlighted throughout; **open on claude.ai ↗**
  jumps to the original, and **in project** assigns the chat to a project

## Ask (optional)

The **Ask** tab sends the excerpts that best match your question to Claude via
the API and returns an answer citing the chats it used. It needs an API key
from the Claude Console (pay-as-you-go, separate from your subscription); put it
under **Settings**. Only the matching excerpts are sent, never the whole archive.
The default model is `claude-sonnet-5`; change it under Settings if you prefer
another.

## Where things live

`%LOCALAPPDATA%\Re-Search\` — `imports\` (your zips) and `index.db` (the
index, plus the API key if you saved one). Delete the folder to start over, or
run `python re_search.py --rebuild` to rebuild the index from the zips.

Other flags: `--port 9000`, `--no-browser`, `--import path\to\export.zip`.

## Projects

Claude's export lists your projects (name, instructions, documents) but does
not record which project each chat belongs to, so every project starts at 0
chats. Two ways to assign them: open a chat and use the **in project**
selector in its header, or tick several chats in the results list (there's a
"select all shown" link next to the result count) and use the bar that
appears above the list to assign them all at once. Counts update as you go and
assignments are kept across re-imports. Each chat's short summary from the
export is shown under its title and is searched together with the title.

The documents you uploaded to projects are indexed too. They appear in search
results with a "file" badge and their project, and the "who said it" dropdown
has a **Project files** option to see only those (or Me / Claude to leave them
out).
