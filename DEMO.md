# Demo: Excel → call → Excel

Judges see one loop: an order waiting in `Sonali_SU_DB.xlsx`, Sonali calling about it, the same row flipping to **Verified/Called = Yes**.

```
Sonali_SU_DB.xlsx  (worksheet Orders)
  Name / Phone Number / Address / quantities / Flagged Concerns
  Verified/Called = Not yet
        │
        ▼
  laptop (cmd)
    picks the first Not yet row that has a Name and a Phone Number
    talks on the laptop speaker and mic
    greets by Name, in Hindi
    asks about the product (from the quantity columns)
    asks the Flagged Concerns / address-gap question  (Missing Pincode, No house number, …)
        │
        ▼
  same workbook, only the cells that case needs
  Verified/Called     Not yet  →  Yes
  Agent Call Review   blank    →  Address updated successfully
                                  (or Manual call needed, could not obtain Address, …)
  Address             updated only when the customer gave a new one
  Name, phone, quantities, Flagged Concerns  unchanged
```

## What each column is for

| Sheet column | Sonali uses it to | Written back? |
|---|---|---|
| **Name** | greet them ("नमस्ते Amrita जी!") | no |
| **Phone Number** | dial, or show before you press Enter | no |
| **Address** | read out, then ask only what is missing | **yes, only when the call updated it** |
| **Herbal Shampoo Quantity (250 ml)** / **Rosemary Conditioner Quantity (250 ml)** | name the product in the confirm question | no |
| **Flagged Concerns** | choose the extra question ("And what is your pincode?") | no |
| **Verified/Called** | skip rows already `Yes`; after a completed call set to `Yes`; leave `Not yet` if nobody answered | **yes** |
| **Agent Call Review** | one sentence for that outcome | **yes** |

A row is pending when **Verified/Called** is `Not yet` (or blank) and both **Name** and **Phone Number** are filled.

## 8-minute hackathon run (recommended)

Close the workbook in Excel first, or the save will fail. Answer as the customer on the laptop mic.

### Before the room (once)

1. `Sonali_SU_DB.xlsx` sits in this folder. Keep **one demo row** on `Not yet`.
2. Wear headphones. Sonali talks on the laptop speaker and listens on the laptop mic.

### On stage

1. **Show the workbook.** Point at the demo row: `Verified/Called = Not yet`, and the Flagged Concerns / thin address (for example a missing pincode).
2. On the laptop, in cmd:

   ```
   python call.py --limit 1 --verbose
   ```

3. Press Enter. Sonali greets in Hindi, follows your language, and asks the gap from **Flagged Concerns** / the Address cell.
4. Answer as the customer after the beep. After she hangs up, **re-open the workbook**: `Verified/Called` is `Yes`, `Agent Call Review` is a sentence for that outcome, and **Address** has the house number / pincode if they gave one. Name, phone and quantities have not moved.

### If you only want to prove the Excel loop (no mic, no phone)

```
python call.py --dry-run --limit 1
```

Then open `Sonali_SU_DB.xlsx`. The pending row shows `Yes` and a review sentence. Add `--speak` to hear her. Set **Verified/Called** back to `Not yet`, clear **Agent Call Review**, and restore Address if it changed, for a second take.

## What to say if they ask

- **Where does the number come from?** The **Phone Number** cell of the first `Not yet` row. `call.py` prints it; it does not place a carrier call.
- **How does she know to ask for a pincode?** **Flagged Concerns** says `Missing Pincode`, or the Address cell has no 6-digit pin. She only asks that gap.
- **What if the family ordered it?** "Yeah, maybe my daughter did it" counts as yes.
- **What if they push back?** "Why should I tell you?" is not rude. She explains and asks again, up to 3 times.
- **What is Agent Call Review?** A one-line outcome for the shop: `Order confirmed`, `Address updated successfully`, `Manual call needed, could not obtain Address`, `Manual review needed, customer denied the order`, `Call ended, inappropriate language`, `No answer, retry needed`.
- **Why is it free?** Whisper, Gemma and Kokoro all run on the laptop. The database is a local Excel file.

## Reset a row for a second take

Set **Verified/Called** back to `Not yet` and clear **Agent Call Review**. Do not touch the other columns. Run `python call.py --limit 1 --verbose` again.
