#!/usr/bin/env python3
"""Preview pending orders, or (optional) save a Google Sheet web-app URL.

  python setup_sheet.py
      reads Sonali_SU_DB.xlsx and prints Name / Phone Number / Flagged Concerns

  python setup_sheet.py https://script.google.com/macros/s/.../exec YOUR_TOKEN
      writes sheet.json (git-ignored) for the optional Google Sheet path
"""
import json
import sys
from pathlib import Path

import sheets
import verify

HERE = Path(__file__).parent
CONFIG = HERE / "sheet.json"


def show(store):
    orders = store.load()
    print(f"{store.describe()}  ({len(orders)} pending)\n")
    if not orders:
        print("No pending rows (Verified/Called is already Yes, or Name/Phone is empty). "
              "Set a row back to Not yet and run again.")
        return
    print(f"{'row':<5} {'name':<22} {'phone':<16} flagged concerns")
    for o in orders:
        print(f"{o['order_id']:<5} {o.get('customer_name','')[:21]:<22} "
              f"{o.get('phone',''):<16} {o.get('address_discrepancies') or '-'}")
        flags = verify.order_flags(o)
        if flags:
            print(f"      script: {', '.join(flags)}")
    print("\nDemo one call with:\n  python call.py --limit 1 --verbose")


def main():
    if len(sys.argv) == 1:
        if not verify.DEFAULT_EXCEL.exists():
            sys.exit(f"No {verify.DEFAULT_EXCEL.name} in this folder.")
        return show(sheets.ExcelOrders(verify.DEFAULT_EXCEL))
    if len(sys.argv) != 3 or "script.google.com" not in sys.argv[1]:
        sys.exit(
            "Usage:\n"
            "  python setup_sheet.py\n"
            "  python setup_sheet.py <web-app-url-ending-in-/exec> <TOKEN>\n"
        )
    url, token = sys.argv[1].strip(), sys.argv[2].strip()
    store = sheets.AppsScriptOrders(url, token)
    try:
        store.load()
    except Exception as e:
        sys.exit(f"Could not read the sheet ({e}).\n"
                 "Check the URL, the TOKEN, and that the web app is deployed "
                 "as Execute as: Me, Who has access: Anyone.")
    CONFIG.write_text(json.dumps({"url": url, "token": token}, indent=2), encoding="utf-8")
    print(f"Saved {CONFIG}\n")
    show(store)


if __name__ == "__main__":
    main()
