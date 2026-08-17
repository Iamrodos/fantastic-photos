# Fantastic Photos

Tidies up photo libraries. Merges several folders into one, renames each file
by the date and time it was taken, and points out duplicates, crops and bursts
along the way.

Runs entirely on your own machine. Your photos are never uploaded anywhere.
**Originals are only ever copied — never moved, renamed or deleted.**

---

## Setting up on Windows

You only need one thing: **uv**. It installs Python and everything else by
itself.

**1. Install uv.** Open PowerShell (press Start, type `powershell`, press Enter)
and paste this line:

```
irm https://astral.sh/uv/install.ps1 | iex
```

Wait for it to finish, then close PowerShell.

**2. Download this folder.** On the GitHub page, click the green **Code**
button, then **Download ZIP**. Right-click the downloaded file and choose
**Extract All**. Put the folder somewhere you'll find it again, such as your
Desktop.

**3. Double-click `run.bat`.**

A black window appears, then your browser opens with the app. Leave the black
window alone while you're using it — closing it stops the app.

That's it. Every time you want to use it, double-click `run.bat`.

### On a Mac

Same, but install uv with:

```
curl -LsSf https://astral.sh/uv/install.sh | sh
```

and double-click `run.command` instead.

---

## Using it

The page walks through six steps, top to bottom.

**1. Folders.** On the left, choose the folders holding your photos — drag them
in from Explorer, or use the browser. On the right, choose where the merged
copies should go.

**2. Options.** Whether to look for crops and bursts, and where to put videos
and images you were sent rather than took.

**3. Scan.** Reads every photo. Counts appear as it works. A few hundred photos
takes a minute or two; a few thousand takes longer.

**4. Review.** Any groups of related photos are shown side by side — bursts,
crops, near-identical shots. Each photo has a *keeping* / *skipped* toggle.
Everything is kept unless you say otherwise, so you can skip this entirely.

**5. Preview.** Every file and the name it will be given. Nothing has happened
yet.

**6. Copy.** Creates the destination folder and copies the files in, with a
`_manifest.csv` recording exactly what came from where.

---

## What the new names look like

```
2024-09-12 14.30.22 IMG_3456.jpg
```

Date, then time, then the camera's original filename. Because the name starts
with the date, your file manager sorts them into the order they were taken with
no software needed.

Photos with no date information are named `undated IMG_3456.jpg` and collect
together at the end of the folder. The tool never guesses a date.

---

## Questions you might have

**Will this change my originals?** No. It only reads them and writes copies
somewhere new. If you dislike the result, delete the new folder and nothing has
been lost.

**What if I run it twice?** You get two folders. Point it at a different
destination each time, or delete the first.

**Why do some photos say "no date"?** Because the information genuinely isn't
in the file. Apps like WhatsApp strip it out when someone sends you a picture.

**What is `_manifest.csv`?** A spreadsheet listing every file, its new name, and
anything that was skipped and why. Open it in Excel. It's your record of what
the tool did.

---

## Updating

`run.bat` checks whether a newer version has been published and asks before
replacing your copy. Say no and it carries on with what you have. The previous
version is kept as `fantastic_photos.previous.py` in case you want it back.

---

## Requirements

- uv (which brings its own Python 3.9+)
- Pillow and numpy — installed automatically by uv on first run
