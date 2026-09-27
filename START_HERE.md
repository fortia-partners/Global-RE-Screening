# Getting RE.SCREEN online — for the four of you, no IT person needed

This is the only guide you need. Ignore the other docs in this folder — they're
written for developers. This one is written for you.

Two things happen below: **putting the tool online** (10 minutes, zero typing),
and **making the numbers real** (one double-click, then wait).

---

## Part 1 — Put it online (do this first)

You'll end up with a real, working web address you can send anyone.

1. On a computer (not a phone), go to **app.netlify.com/drop**
2. You do not need to make an account for this step.
3. Find the **`rescreen-light`** file in this folder (or wherever you downloaded
   it from our chat) and **drag it onto the page** where it says to drop files.
4. Wait about 10 seconds.
5. Netlify shows you a link, something like `glowing-narwhal-4f2a1c.netlify.app`.
   **That's it — that's your website.** Click it to see it live.

Send that link to your three colleagues. Anyone who opens it will see a
passcode screen first (see below) — that's expected.

**To make the link nicer**, click "Site settings" on that same Netlify page and
change the site name — it becomes `yourname.netlify.app` instead of the random
one. Not required, just tidier.

**To update it later** (after you refresh the data — see Part 2), go back to
your Netlify page, and drag the folder onto it again. It replaces the old
version. Same link, new numbers.

### The passcode screen

Everyone who opens the link sees a box asking for a passcode before the tool
appears. This keeps it from looking like a public, unlabelled page if the link
ever gets shared outside the four of you — it is **not** a real lock, just a
"this is private, Nilus-only" sign.

The passcode is currently set to **`nilus2026`**. To change it: open the
`index.html` file in any plain text editor (right-click → Open With → Notepad,
or TextEdit on Mac), press Ctrl+F (Cmd+F on Mac) and search for
`GATE_PASSCODE`. Change the word in quotes to whatever you want, save, and
re-drag the folder onto Netlify.

If in the future your firm wants a real login system — one that can't be
bypassed by anyone who views the page's source code — that needs a different,
paid setup. Worth doing only once this has proven useful. Ask if you get there.

---

## Part 2 — Make the numbers real

Right now, every price and yield in the tool is fake test data — there's a
yellow **DEMO DATA** badge confirming it. This part replaces that with real
numbers.

**One of you needs to do this once.** After that, anyone can repeat it monthly
by clicking the same file.

### One-time setup (5 minutes, only needed once ever)

Check if Python is already on your computer:

- **Mac:** open Spotlight (Cmd+Space), type `Terminal`, press Enter. Type
  `python3 --version` and press Enter. If you see a version number, skip ahead.
- **Windows:** open the Start menu, type `cmd`, press Enter. Type
  `python --version` and press Enter. If you see a version number, skip ahead.

If you don't see a version number:

1. Go to **python.org/downloads**
2. Click the big **Download Python** button
3. Open the file you downloaded
   - **Windows only:** on the very first screen of the installer, tick the box
     that says **"Add python.exe to PATH"** before clicking Install. This is
     the one step people miss — if you skip it, everything below fails with a
     confusing error and the fix is to reinstall and tick the box.
   - **Mac:** just click Continue through the installer, defaults are fine.

### Every time you want fresh numbers

1. Find the file called **`UPDATE PRICES.command`** (Mac) or
   **`UPDATE PRICES.bat`** (Windows) in this folder.
2. Double-click it.
   - **Mac only, first time:** macOS will refuse and show a warning, because
     the file wasn't downloaded from the App Store. This is normal. Right-click
     the file instead, choose **Open**, then click **Open** again on the popup.
     You only have to do this the first time.
3. A black window opens and fills with text. **This is normal — leave it
   alone.** It's checking prices and financials for roughly 900 companies
   across 20+ countries, which takes 30 to 60 minutes.
4. When it says **DONE**, close the window.
5. Go back to **app.netlify.com/drop** and drag this same folder onto the page
   again. That publishes the update to your existing link.

**If the black window shows an error and stops:** don't panic, nothing is
broken. Take a screenshot of the whole window and share it in this chat — most
errors are a single missing piece that takes one message to fix, and running
it again afterward is always safe.

---

## What this gives you, honestly

**Prices update whenever someone double-clicks the file** — not automatically,
not every minute. For a monthly or weekly review that's normal and fine. If you
later want prices that refresh every hour without anyone clicking anything,
that's an automated version a developer can set up — see `DEPLOY.md` — but it's
not required to have a fully working tool today.

**True real-time, tick-by-tick prices don't exist for free**, for any of the
20+ countries here except partially the US. Free data is end-of-day to about
15 minutes delayed almost everywhere in Europe and Asia — that's an exchange
licensing rule, not something any amount of engineering fixes. What you're
building is accurate as of each update, which is what a monthly or weekly
screening tool actually needs.

**The passcode is a courtesy, not a lock.** Anyone who really wanted to bypass
it technically could. For an internal tool shared only among four partners,
that's a reasonable tradeoff against the alternative — which is a much bigger
project none of you would be able to maintain yourselves.
