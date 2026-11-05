# PageWatch user guide

PageWatch checks web pages (and PDFs, feeds, files and more) on a schedule, ignores the noise,
and shows you what changed. Everything stays on your computer: no account, no cloud, no
telemetry.

It is two programs that work together:

- the **engine** does the checking. It runs in the background, with a small icon in the
  notification area, and keeps going when you close the window or sleep the laptop;
- the **window** is where you add pages and read changes. Open and close it whenever you like.

## Install

Run `PageWatch-Setup-<version>.exe`. It installs for your own account (no administrator needed;
choose "for all users" in the first dialog if you prefer). Leave **Start PageWatch when I sign
in** ticked and the engine starts by itself every time you log on and restarts if it ever stops.

Windows may warn about an unrecognised publisher the first time: choose "More info", then "Run
anyway". The build writes a `.sha256` file next to the installer so you can check the download.

Prefer no installer? Unpack `PageWatch-<version>-portable.zip` anywhere and double-click
`Start PageWatch.cmd`. See "Portable copy" below.

## Add your first page

1. Press **Ctrl+N** (or **Add**) and paste the address of a page.
2. The assistant fetches the page and shows what PageWatch makes of it. It fetches twice, so
   anything that already differs between the two (a clock, a session token) is proposed as
   something to ignore. If the page needs JavaScript to show its content, PageWatch notices and
   switches to a real browser by itself. You can also choose to watch only one region.
3. Pick how often to check ("Check every") and save.

The first check records the page as it is now. After that, each change is stored, and a Windows
notification tells you what changed.

## Read your changes

The list works like an email client: unread pages are in bold. Select one and the viewer shows
the change since you last marked it read. Its tabs are **Highlighted** (the page with changes
marked), **Text diff**, **New**, **Old**, **Screenshot diff** (for pages you watch visually) and
**Check log**.

Everything works from the keyboard:

- **N** or **Space**: next unread
- **R**: mark read
- **O**: open the page in your browser
- **C**: check the selected page now (**Ctrl+Shift+C** checks everything)
- **F**: flag the change as a false positive
- **Ctrl+E**: edit the bookmark
- **Ctrl+F**: search; **Ctrl+1** to **Ctrl+6**: the viewer tabs
- **Ctrl+P**: pause or resume AutoWatch (all automatic checking)

## Cut the noise

Pages are full of things that change without meaning anything: dates, counters, ads, cookie
banners. PageWatch removes the common ones by itself. For the rest:

- When an alert is not worth it, press **F**. PageWatch proposes a filter that would have hidden
  it, and **Test filter** shows the effect before you keep it.
- In the editor you can watch only part of a page, ignore parts of it, or set a minimum amount of
  change before you are alerted.
- **Keywords** alert only when your words appear in what changed. For example
  `page("RTX 4090") + num(\$([\d,.]+)) < 1200` alerts when a price below 1200 appears on a page
  that mentions the RTX 4090.

## Alerts

Every bookmark shows a Windows notification by default. Email and ntfy (phone push) are
available per bookmark. Mark a bookmark **private** and its alerts never include page content.

## The engine and the notification-area icon

The icon shows the state: normal, unread changes, paused, offline or error. Right-click it for
**Open PageWatch**, **Check all now**, **Pause AutoWatch** (for an hour or until resumed) and
**Quit engine**. Quitting the engine stops all checking until it is started again (by the
window, at your next sign-in, or by running `pagewatch-engine`).

While your computer sleeps or is offline PageWatch does not count that as errors; when it
wakes it catches up, staggering the overdue checks.

## Your data

Everything PageWatch knows is in one folder: `%LOCALAPPDATA%\PageWatch`.

- `pagewatch.db` and `blobs\` are your bookmarks, settings and page history;
- `logs\engine.log` is the first place to look when something is wrong;
- `backups\` holds the automatic daily backups and the safety copies made before upgrades.

To move to another computer, make a backup that includes the page history (`pagewatch-cli
backup`, see `--help` for the option) and restore it there (`pagewatch-cli restore <file>`).
Passwords are never in backups; PageWatch lists which ones to enter again.

## Upgrade, uninstall

**Upgrade:** run the new installer over the old one. It stops the engine, replaces the program,
and starts it again. Your data is upgraded the first time the new engine starts, after it has
saved a copy of the old database as `backups\pre-migration-v<N>-<date>.db`. An older PageWatch
will refuse to open data that a newer one has upgraded; it never changes it.

**Uninstall:** use "Apps" in Windows Settings. The start-at-sign-in task is removed. Your data
folder is kept unless you answer Yes when asked whether to delete it.

## Portable copy

The portable zip needs no installation and writes nothing outside its own folder: your data is
in `data\` next to the programs. Copy the folder to another PC or a USB stick, or copy it twice
to run two completely separate PageWatches side by side. It does not start at sign-in; see
`README-portable.txt` if you want that. Any program takes `--data-dir <folder>` to use a
different data folder.

## Command line

`pagewatch-cli` controls the running engine, from a terminal or a Task Scheduler entry:

    pagewatch-cli add https://example.com/news --name "Example news" --interval 15m
    pagewatch-cli list --unread
    pagewatch-cli check --all
    pagewatch-cli read 12
    pagewatch-cli status

Run `pagewatch-cli --help` for everything else.

## When something is wrong

- **"The engine is not running"** in the window: choose Yes to start it. If it will not start,
  read `logs\engine.log`. A second engine on the same data folder refuses to start by design.
- **A page that needs JavaScript shows nothing:** PageWatch uses Microsoft Edge, which Windows
  already has. If Edge cannot be used it downloads its own browser the first time one is
  needed, so the first such check can take a minute.
- **Antivirus or SmartScreen complains:** the programs are not code-signed. Compare the
  installer's SHA-256 with the `.sha256` file that came with it.
- **Nothing is reachable from the network:** the engine listens only on this computer
  (`127.0.0.1`), so no firewall rule is needed and nothing outside can connect to it.
