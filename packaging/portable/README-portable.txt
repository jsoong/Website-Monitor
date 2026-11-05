PageWatch, portable
===================

This folder is a complete PageWatch. Nothing is installed and nothing is written outside it:
your bookmarks, page history, settings and logs live in the "data" folder next to the programs.

Start it
--------
Double-click "Start PageWatch.cmd". It starts the engine (which does the monitoring) for the
"data" folder here, then opens the window. Closing the window does not stop monitoring. To stop
the engine, right-click its icon in the notification area and choose "Quit engine".

Move it, copy it, run two
-------------------------
* To move to another PC or a USB stick, copy the whole folder (stop the engine first).
* To run a second, separate PageWatch, copy the folder and start the copy. Each folder has its
  own data and its own engine, and they can run at the same time.
* To point the programs at any other data folder:
      pagewatch-engine.exe --data-dir D:\somewhere\data
      pagewatch-ui.exe     --data-dir D:\somewhere\data
      pagewatch-cli.exe    --data-dir D:\somewhere\data list

Starting at sign-in
-------------------
A portable copy does not start by itself. If you want that, run once, from this folder:

      pagewatch-cli.exe --data-dir "%CD%\data" service install --exe "%CD%\pagewatch-engine.exe"

and "pagewatch-cli.exe service uninstall" to undo it. Moving the folder afterwards means
running the install command again from the new place.

Upgrading
---------
Stop the engine, then unpack the new version over this folder (or into a new one) and keep the
"data" folder. The engine upgrades the data when it starts, after saving a copy of the old
database in data\backups.

More: USER_GUIDE.md in this folder.
