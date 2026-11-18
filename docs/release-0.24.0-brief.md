# SnapSTL 0.24.0: what changed (brief)

Released 2026-11-18. It is on GitHub `main`. **No computer has it yet.** Each one gets it through
**Settings → Updates** (see "Update day" at the end).

---

## 1. Print → Verify: a person checks every print

**What you see**
- Print now has five tabs: **Bases · Names · Verify · Completed · Removed**.
- When Bambu reports a plate as printed, it goes to **Verify**, not straight to packing.

**How to check a plate**
1. Choose your name in **Checked by**.
   - **Add name** adds a staff member. The list is shared with every computer.
   - A name can be hidden, but it is never removed, so old checks keep the name.
2. Take the plate off the printer and look at every base and name.
3. Mark each one:
   - **Good**
   - **Letter(s) spoiled**: tap the bad letters on the picture. They turn red.
   - **Whole name spoiled**
   - **Wrong colour**
   - **Base broken or warped**
4. Press **Save check**.

**What happens after a check**
- Each problem makes a small red **Fix** plate.
  - It holds only what was spoiled, at exactly the same size.
  - It is cut from the plate file, so nothing is downloaded or scaled again.
  - Fix plates sit at the top of Bases / Names and are printed and checked like any plate.
- **Whole plate failed** sends the plate back to print again.
- An order moves to **Printed – pack it** only when its base and its name are both checked good.
  - Until then, Orders shows **Printed – waiting for check**.
- If the same name was fixed twice before, the check shows a warning to look at the model.
- Everything printed before this update counts as checked by **"Before 0.24"**, so no order moves.
- An order cancelled while printing shows **Cancelled – don't pack**.

## 2. Completed tab

- Lists the names checked good and not shipped yet:
  - colour, order, batch, printer
  - **who checked it** and when
  - a "had a fix" label where a fix was needed
- Filter by date (Today, Last 7 days, …) or search by name or order.
- **Packing slip**: tick rows (or none, for all shown) and print a list for packing.
- **Found a problem…**: for a fault noticed later, e.g. while packing. It makes a fix plate and takes the order back to Printing.

## 3. Merge plates, and new file names

- **Merge**: on **Names**, a yellow bar appears when same-colour plates that aren't started have room (up to 3 names).
  - **Merge onto one plate** puts them on one plate. Nothing is scaled.
  - The single plates leave the list.
  - **Split back** undoes it until the merged plate is started.
  - The merged file goes in the newest batch's `Merged/` folder.
- **Group by colour** on Names: load one spool and print every plate of that colour.
- **New file names**:

  | Plate | File name |
  |---|---|
  | Bases | `Bases - Ayra + Manvi + Riya.3mf` |
  | Names | `Pink - Ayra + Manvi.3mf` |
  | Fix | `Fix - Pink - Riya (r).3mf` |
  | Merged | `Merged - Pink - Ayra + Tara.3mf` |

  - They use a plain "-" so printers' SD cards accept them.
  - Plates built before this update keep their old names.

## 4. A print never starts on a full bed

- If a printer shows a finished print, **Start print** asks you to take it off and confirm the bed is empty.
  - Confirming also clears the plate in Bambuddy.
- If the printer is busy with another print, the new plate waits *held* in its queue. It never starts by itself after that print.
  - Press **Start print** again once the bed is empty.
- **Queue download** and **Queue build** still start only when someone presses them.
- The Printers tab is unchanged.

## 5. Batches page

**The table**
- Each batch shows:
  - the **names** as colour chips. Shipped names are faded and cancelled ones are struck through.
  - a **progress** bar for each step: Printed / Checked / Packed / Shipped
  - its **status in words**: Downloading → Building → Ready to print → Printing → Checking → Packing → Shipping → All shipped
- Batches are grouped by day: Today, Yesterday, then dates.
- Filters: date (Today, 7 days, This month, pick dates), status, and search by name or order.
- The Computer column is hidden while only one computer makes batches.

**Archive and clean-up**
- A batch moves to **Archive** once all its orders are **On the way**, Delivered, Coming back or Cancelled.
- **10 days later** its folder goes to the **Recycle Bin**. The Batches page still lists it, marked "files in the Recycle Bin".
- Downloaded models no batch has used for **15 days** also go to the Recycle Bin.
  - A model is kept if an order still waiting to be made has that name.
  - If a model is needed later, it is downloaded again.
- Nothing is ever deleted for good, and nothing is moved while a batch is downloading or building.
- **Settings → Maintenance → Disk space and old files** shows:
  - the space used
  - what the next clean-up will move
  - a **Clean up files now** button

## 6. Other changes

- **Activities** tab, below Settings:
  - a plain list of what happened: prints, checks (with who checked), builds, downloads, merges, clean-ups
  - filters by type and date
  - the **Download queue** is here now
- **Orders → ↻ Refresh**, next to *Add ad-hoc order*: loads the latest from every computer and shows it.
- **Cleaner pages**: long explanations sit behind a **?** next to each heading. Click it to read them.
- **Logistics page**:
  - shows *Printed – waiting for check* for those orders
  - *Labels to make* lists only orders that were checked good

## 7. How it was checked

- 137 automated tests pass, including syncing between two computers through a test Supabase.
- Practice run on a copy of your database:
  - 116 already-printed items were counted as checked
  - all 1,670 orders stayed in the same tab
  - a second, empty computer received every order in the same tab, plus all the checks
  - a 0.23 computer is refused
- On a copy, letter splitting works on real names:
  - SuriPari splits into 8 letters, Ronika into 6, ANIKA into 5
  - a fix plate made with only the "n" of Ronika held just that letter

**Not tested on the real printers.** These need the real Bambuddy and printers, so check them on the first prints after updating:
- the bed-clear question
- whether Bambu Studio opens the fix and merged plates normally

## Update day

Do this the same day on every computer. A 0.23 computer stops syncing once one computer is on 0.24.

1. Make sure no batch is downloading or building.
2. **Settings → Maintenance → Back up now**.
3. **Settings → Updates → Update now**. Update the logistics host first, then the other computers.
4. On the production computer, add the staff names: **Print → Verify → Add name**.

The first check after updating shows who checked the plate everywhere: Orders, Completed, Activities, and the shared records.
