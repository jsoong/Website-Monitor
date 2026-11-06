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

## Pages behind a login

Some pages show their content only once you are signed in. PageWatch can check them, and your
password never sits in the open: it goes to the Windows credential store, the field you typed it
in empties itself, and it is never shown again. Everything below is on the **Login** tab of the
bookmark's editor (**Ctrl+E**). Pick the way that fits the site:

- **The site has an ordinary sign-in page, perhaps with a one-time code or a CAPTCHA:** *sign in
  by hand*. It is the most dependable choice, and the only one that copes with a code.
- **The site has an ordinary sign-in page and nothing extra:** a *macro* can sign in for you.
- **The browser itself pops up a "Sign in" box** (a router, an intranet page, some feeds): a
  *server login*, Basic or Digest.
- **You are already signed in in your own browser:** *import its cookies*.

A bookmark that is behind a login gets private alerts by default: they say which bookmark
changed, not what it says. Changing **Alert content** back to **Include the changes** asks you to
confirm, because the alert would then carry text from your signed-in pages.

A macro or a hand sign-in always checks through the browser, which adds about a second to each
check. A server login and cookies work with the quicker page fetch as well.

### Sign in by hand

1. On the Login tab tick **I sign in by hand (a browser window opens for me)** and save.
2. Choose **Sign in...** (the toolbar, **Ctrl+L**, or the button on the Login tab). A browser
   window opens on a profile that belongs to this bookmark alone. Sign in as you normally do,
   including any one-time code.
3. Close the window, or press **I'm signed in**. PageWatch checks the page at once.

Later checks reuse that session in the background and need nothing from you. While the window is
open, scheduled checks of that bookmark wait (they try again 30 seconds later). If the sign-in
page is not at the bookmark's own address, put it in **Sign-in window opens at**. **Wipe
session** makes PageWatch forget the saved session.

Sessions do run out. Once a day (at 09:00, or when the computer next wakes) PageWatch offers a
fresh sign-in for hand-signed-in bookmarks you have not signed in to for 20 hours: a line in the
status bar, a count on the **Sign in...** button, and one notification, never a window that
takes focus. Change the time, or turn the offer off, in **Settings > Network** (**Offer to sign
in again once a day**, **Daily sign-in offer at**).

### Server login (Basic or Digest)

Tick **Sign in to the server with a user name and password**, choose **Basic** or **Digest**
(whichever the server uses; Basic is the usual one), enter the **User name**, type the
**Password** and press **Save password**. **Password name** is only the label the password is
kept under in the credential store; PageWatch suggests one. The line under it says whether a
password is stored. To change it, type the new one and save again.

Basic sends the password with every request, so PageWatch refuses to use it over plain
`http://` to a site on the internet. Use `https://`; your own computer and addresses on your
own network are fine.

### Cookies

If you are signed in in your normal browser, export that site's cookies as a **Netscape
`cookies.txt`** file (browser extensions that export `cookies.txt` write this format, and so does
`curl`), then press **Import cookies.txt...**. PageWatch shows how many cookies it read, for which
sites, how many have already expired and when the first one will. **Remove** deletes them.

- A cookies file is as good as your password for that site. PageWatch copies it into its data
  folder, readable by your account only, and never shows the values. Delete the exported file
  once you have imported it.
- When the cookies expire, or the site stops accepting them, the bookmark needs you (see "When
  a sign-in runs out"). Import fresh ones.
- PageWatch does not write cookies a site sets during a check back to the file. For a site
  that replaces its session cookie on every visit, sign in by hand instead.

### Macros

A macro is a short list of steps PageWatch performs in the browser when a check lands on the
site's sign-in page: go to an address, fill in a field, click, check that it worked. Macros have
names and can be shared by several bookmarks. Open them with **Macros** on the toolbar, or pick one
in the **Macro** list on a bookmark's Login tab (the **Macros...** button next to it opens the
same window).

**Record one:** choose **Record...**, enter the sign-in page under **Start page**, and press
**Start recording**. A browser window opens: sign in the way you normally do, then close it. The
recorded steps appear for review; press **Use these steps**, give the macro a **Name**, say how
the sign-in page is recognised (below) and **Save**.

**Passwords are never recorded.** A password you type while recording is replaced by a
reference to a stored password, named in **Password is stored as**. In the table **Passwords used
by these steps** press **Set password...** to store it. The steps themselves are a list you can
edit as text. The kinds of step are `goto`, `fill`, `click`, `select`, `press`, `wait_for` (a
selector, or a number of milliseconds) and `assert_text`; **New** shows an example. A password
field cannot hold a password directly, only a reference.

**Recognising the sign-in page** is what keeps most checks from signing in at all: the macro runs
only when one of these shows up. Fill in whichever fits:

- **Login page has**: a CSS selector for something only the sign-in page contains, such as
  `input[type=password]`;
- **or the address has**: part of the address you are sent to, such as `/login`;
- **or the page says**: a phrase on that page, such as `Please sign in`.

Signing in with a stored password on its own is deliberately hard to switch on, because
repeated automatic logins can get an account locked or flagged:

- It is off until you turn on **Allow stored-password logins** in **Settings > Network**, and the
  bookmark must opt in too with **Let the macro sign in with a stored password on its own**.
- Even then it signs in at most once per **At most one automatic sign-in per** (6 hours by
  default; 1 hour at the least), and **never again after a failure** until you fix the cause and
  press **Resume checks**. The failed attempt still counts as that period's one sign-in, so after
  correcting a wrong password you may wait for the period to pass.
- Leave the global switch off for ACCESS HRA and Housing Connect. The test that shows whether
  automatic sign-in is safe there has not been run (`pagewatch-cli portal-experiment --help`
  describes it; `docs/DECISIONS.md` has the details).

A macro that does not use a stored password (it dismisses a consent wall, or types a public
code) is not affected by these rules.

### When a sign-in runs out

All of the above share one rule. When a check lands on the sign-in page, or the server answers
"unauthorised" to a saved login or cookies, the bookmark changes to **needs sign-in**:

- its automatic checks pause, and it does not set off the alert that a page keeps failing;
- you get **one** alert, which names the bookmark and nothing about the page, and it is listed
  in **Problems**;
- retrying would not help and could lock the account, so PageWatch does not try again by itself.

Fix the cause (**Sign in...**, import new cookies, correct the stored password, repair the macro).
Importing cookies or signing in resumes the bookmark by itself; otherwise press **Resume checks**
on the Login tab or in **Problems**. **Check now** still works while a bookmark is paused. The
Login tab says in words why the bookmark stopped, for example "the macro did not get past the
login page (step 3 ...)".

## Proxies

To send checks through a proxy, put its address in **Settings > Network > Proxy** (every bookmark
uses it) or, for one bookmark, in **Proxy** on the editor's **Advanced** tab (this one wins over
the global setting). Use `http://host:port`, `https://host:port` or `socks5://host:port`.

If the proxy asks for a login, give the **Proxy user name**, type the password into **Proxy
password** and press **Save password**; the **Proxy password name** is the label it is kept
under. Do not put the user name or password inside the address: PageWatch refuses an address
that has them. A SOCKS5 proxy that needs a login works for ordinary page checks, but the browser
cannot sign in to one, so a browser check through it stops with a message saying so.

## Being polite to a website

PageWatch never hammers a site. By default it makes at most **2** checks at once to the same
website, at least **2 seconds** apart. The website is the address's host name, so `example.com`
and `www.example.com` count as different websites. Change the defaults on **Settings > Network**
(**At once per website**, **Minimum gap per website**).

**Per-website exceptions** are the table under them. Press **Add website**, type the site's name
(pasting a whole address is fine, it is cut down to the host name) and fill in **At once**, **Minimum
gap (s)** or both. An empty cell uses the default. Use it to be gentler with a small site, or to
allow more for your own server. **Remove selected** deletes a row.

When the bookmarks on one website together ask for more checks a minute than its minimum gap
allows (a hundred bookmarks that each want checking every minute, on a site with a two-second
gap), PageWatch does not let a queue build up. It makes every one of that website's intervals
the same number of times longer, so the checks happen less often than the schedules say. Not
affected: **Check now**, schedules with fixed times, and the first check of a new bookmark. The
bookmark editor warns when its website is in this state, and **Settings > Capacity** lists each
website's demand against its limit, with the factor (for example `x2.5`) under **Intervals**.
Turn the slowing off with **Slow a website's checks evenly when it is asked for too many**
(then the checks queue behind each other instead), or lengthen the intervals or shorten the gap.

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
Passwords are never in backups; PageWatch lists which ones to enter again. Imported cookies and
the browser sessions from signing in by hand are not in backups either, so on the new computer
import the cookies again and sign in again.

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
- **A bookmark says it needs you to sign in:** its checks are paused until you sign in again.
  See "When a sign-in runs out" above.
- **A check through a proxy fails:** read the message in the **Check log** tab. A proxy login
  needs its password stored (**Save password** next to the proxy), and the browser cannot sign
  in to a SOCKS5 proxy.
- **Nothing is reachable from the network:** the engine listens only on this computer
  (`127.0.0.1`), so no firewall rule is needed and nothing outside can connect to it.
