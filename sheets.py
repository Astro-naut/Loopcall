"""Where orders come from and where call outcomes go.

Stores share the same two methods:
  load(only=None, redo=False, limit=None) -> order dicts still waiting for a call
  mark(order_id, fields)                  -> write the outcome onto that row

ExcelOrders      a local .xlsx workbook (the shop database). Only Verified/Called
                 and Agent Call Review are written; every other cell is left alone.
CsvOrders        a local CSV file.
AppsScriptOrders a Google Sheet, through the Apps Script web app in
                 google_sheets/Code.gs (optional; the demo uses Excel).

Sheets don't have to use the app's column names: common headers are mapped
(see ALIASES), a missing order id falls back to the row number, and products
can come from "<Product> Quantity" columns.
"""
import csv
import json
import os
import re
import urllib.parse
import urllib.request
from pathlib import Path

OUTCOME_COLUMNS = (
    "call_done", "call_status", "customer_language", "called_at",
    "address_discrepancies", "address_notes", "call_notes", "agent_review",
)

# Shop columns that a call is allowed to touch. A given outcome writes only
# the subset that case needs (see shop_columns).
SHOP_WRITE = ("call_done", "agent_review", "address")

# Headers people actually use, mapped to the names the app expects.
ALIASES = {
    "order_id": ("order_id", "order id", "orderid", "order no", "order no.", "order number",
                 "order #", "id"),
    "customer_name": ("customer_name", "customer name", "customer", "name", "full name"),
    "phone": ("phone", "phone number", "mobile", "mobile number", "contact", "contact number",
              "phone no", "phone no.", "mobile no"),
    "address": ("address", "shipping address", "delivery address", "full address"),
    "product": ("product", "product name", "item", "items", "product(s)"),
    "amount": ("amount", "price", "total", "order total", "order value", "amount (rs)",
               "amount (₹)"),
    "call_done": ("call_done", "call done", "reviewed/called", "verified/called",
                  "verified called", "verified", "called", "reviewed", "called?", "call made"),
    "agent_review": ("agent_review", "agent call review", "agent review", "call review",
                     "needs review", "review"),
    "address_discrepancies": ("address_discrepancies", "address discrepancies",
                              "flagged concerns", "flagged concern", "flags", "concerns",
                              "address issues"),
}
_CANONICAL = {alias: name for name, aliases in ALIASES.items() for alias in aliases}
# "Rosemary Conditioner Quantity (250 ml)" and "Herbal Shampoo Quantity"
_QUANTITY = re.compile(r"^(.+?)\s*(?:qty|quantity)(?:\s*\([^)]*\))?\s*$", re.I)


def canonical(header):
    key = " ".join(str(header).strip().lower().replace("_", " ").split())
    return _CANONICAL.get(key) or _CANONICAL.get(key.replace(" ", "_")) or str(header).strip()


def _cell_text(value):
    """Excel stores phone numbers as ints; don't turn 8920871726 into '8920871726.0'."""
    if value is None:
        return ""
    if isinstance(value, bool):
        return "TRUE" if value else "FALSE"
    if isinstance(value, int):
        return str(value)
    if isinstance(value, float):
        return str(int(value)) if value.is_integer() else str(value)
    return str(value).strip()


def _normalise(row, fallback_id):
    """Sheet row -> order dict with the app's names. Also returns which sheet
    header each app name came from, so outcomes can be written back there."""
    order, source = {}, {}
    products = []
    for header, value in row.items():
        if not header or str(header).startswith("_"):
            continue
        value = _cell_text(value)
        name = canonical(header)
        qty = _QUANTITY.match(str(header).strip())
        if qty and name == str(header).strip():
            n = int(value) if value.isdigit() else 0
            if n > 0:
                products.append(qty.group(1).strip() if n == 1 else f"{n} {qty.group(1).strip()}")
            continue
        order[name] = value
        source[name] = str(header)
    if not order.get("product") and products:
        order["product"] = " + ".join(products)
    order.setdefault("product", "")
    order.setdefault("amount", "")
    order.setdefault("address", "")
    order.setdefault("customer_name", "")
    if not order.get("order_id"):
        order["order_id"] = str(fallback_id)
    return order, source


def _is_order(order):
    """Need both a name (to greet) and a phone (to dial). Incomplete rows are skipped."""
    return bool(order.get("phone") and order.get("customer_name"))


_DONE_WORDS = ("DONE", "YES", "CALLED", "REVIEWED", "COMPLETED", "COMPLETE")


def _pending(orders, only, redo, limit=None):
    if only:
        orders = [o for o in orders if str(o.get("order_id")) == str(only)]
    elif not redo:
        orders = [o for o in orders if (o.get("call_done") or "").strip().upper() not in _DONE_WORDS]
    if limit:
        orders = orders[: max(0, int(limit))]
    return orders


def review_text(fields):
    """One sentence for the Agent Call Review cell, matching what happened."""
    status = fields.get("call_status") or ""
    notes = (fields.get("call_notes") or "").lower()
    if status == "CONFIRMED":
        return "Order confirmed"
    if status == "CONFIRMED_ADDRESS_UPDATED":
        return "Address updated successfully"
    if status == "DENIED_REVIEW":
        return "Manual review needed, customer denied the order"
    if status == "ENDED_INAPPROPRIATE":
        return "Call ended, inappropriate language"
    if status == "NO_ANSWER":
        if "not picked" in notes:
            return "No answer, phone not picked up"
        if "dropped" in notes:
            return "No answer, line dropped"
        return "No answer, retry needed"
    if status == "NEEDS_HUMAN_REVIEW":
        if "missing_pincode" in notes:
            return "Manual call needed, could not obtain pincode"
        if "no_house_number" in notes:
            return "Manual call needed, could not obtain house number"
        if "address_too_short" in notes:
            return "Manual call needed, could not obtain Address"
        if "still need checking" in notes:
            return "Manual review needed, address still incomplete"
        if "address" in notes:
            return "Manual call needed, could not obtain Address"
        if "unsure" in notes:
            return "Manual review needed, customer unsure who placed the order"
        if "hung up" in notes:
            return "Manual call needed, customer hung up"
        if "no clear answer" in notes:
            return "Manual call needed, no clear confirmation"
        return "Manual review needed"
    return ""


def shop_columns(fields):
    """Only the shop cells this outcome should change.

    Verified/Called stays Not yet on no-answer (so the next run retries).
    Address is rewritten only when the call actually updated it.
    """
    status = fields.get("call_status") or ""
    review = review_text(fields)
    if status == "NO_ANSWER":
        out = {"call_done": "Not yet"}
        if review:
            out["agent_review"] = review
        return out
    if not status:
        return {}
    out = {"call_done": "Yes", "agent_review": review}
    if status == "CONFIRMED_ADDRESS_UPDATED" and fields.get("updated_address"):
        out["address"] = fields["updated_address"]
    return {k: v for k, v in out.items() if v or k == "call_done"}


def shop_write_fields(fields):
    """Back-compat wrapper: Verified/Called + Agent Call Review values."""
    cols = shop_columns(fields)
    return {"call_done": cols.get("call_done", "Not yet"),
            "agent_review": cols.get("agent_review", "")}


def _rename(fields, source):
    merged = {**fields, **shop_columns(fields)}
    return {source.get(k, k): str(v) for k, v in merged.items()}


class CsvOrders:
    def __init__(self, path):
        self.path = path
        self.source = {}

    def describe(self):
        return self.path

    def _read(self):
        with open(self.path, newline="", encoding="utf-8-sig") as f:
            reader = csv.DictReader(f)
            return list(reader.fieldnames or []), list(reader)

    def load(self, only=None, redo=False, limit=None):
        _, rows = self._read()
        orders = []
        for line_no, row in enumerate(rows, start=2):
            order, self.source = _normalise(row, line_no)
            if _is_order(order):
                orders.append(order)
        return _pending(orders, only, redo, limit)

    def mark(self, order_id, fields):
        header, rows = self._read()
        fields = _rename(fields, self.source)
        id_col = next((h for h in header if canonical(h) == "order_id"), None)
        for col in fields:
            if col not in header:
                header.append(col)
        for line_no, row in enumerate(rows, start=2):
            row_id = (row.get(id_col) or "").strip() if id_col else str(line_no)
            if row_id == str(order_id):
                row.update(fields)
        # utf-8-sig so Excel shows Hindi notes correctly
        with open(self.path, "w", newline="", encoding="utf-8-sig") as f:
            w = csv.DictWriter(f, fieldnames=header)
            w.writeheader()
            w.writerows(rows)


class AppsScriptOrders:
    """Talks to google_sheets/Code.gs deployed as a web app.

    Apps Script answers on script.google.com with a 302 redirect to
    script.googleusercontent.com; urllib follows it (POST becomes GET, which is
    exactly what Apps Script expects after doPost has run)."""

    def __init__(self, url, token, timeout=60):
        self.url = url
        self.token = token
        self.timeout = timeout
        self.source = {}
        self.rows = {}

    def describe(self):
        return "Google Sheet (Apps Script web app)"

    def _call(self, req):
        with urllib.request.urlopen(req, timeout=self.timeout) as resp:
            data = json.loads(resp.read().decode("utf-8"))
        if data.get("error"):
            raise RuntimeError(f"Google Sheet said: {data['error']}")
        return data

    def load(self, only=None, redo=False, limit=None):
        query = urllib.parse.urlencode({"token": self.token, "action": "orders"})
        data = self._call(urllib.request.Request(f"{self.url}?{query}"))
        orders = []
        for row in data.get("orders", []):
            order, self.source = _normalise(row, row.get("_row", ""))
            if _is_order(order):
                self.rows[order["order_id"]] = row.get("_row")
                orders.append(order)
        return _pending(orders, only, redo, limit)

    def mark(self, order_id, fields):
        body = json.dumps({"token": self.token, "action": "update",
                           "order_id": str(order_id), "row": self.rows.get(str(order_id)),
                           "fields": _rename(fields, self.source)},
                          ensure_ascii=False).encode("utf-8")
        self._call(urllib.request.Request(
            self.url, data=body, method="POST",
            headers={"Content-Type": "application/json"}))


class ExcelOrders:
    """Local .xlsx shop database. Reads every column; writes only the cells this
    outcome needs (Verified/Called, Agent Call Review, and Address when updated)."""

    def __init__(self, path, sheet=None):
        self.path = Path(path)
        self.sheet_name = sheet
        self.source = {}
        self.rows = {}

    def describe(self):
        return str(self.path)

    def _load_wb(self):
        try:
            from openpyxl import load_workbook
        except ImportError:
            raise RuntimeError("openpyxl is needed to read Excel files. Run: pip install openpyxl")
        if not self.path.exists():
            raise FileNotFoundError(self.path)
        return load_workbook(self.path)

    def _ws(self, wb):
        if self.sheet_name and self.sheet_name in wb.sheetnames:
            return wb[self.sheet_name]
        if "Orders" in wb.sheetnames:
            return wb["Orders"]
        return wb[wb.sheetnames[0]]

    def _headers(self, ws):
        """canonical name -> (1-based column, original header text)."""
        out = {}
        for col in range(1, (ws.max_column or 1) + 1):
            raw = ws.cell(1, col).value
            if raw is None or not str(raw).strip():
                continue
            out[canonical(raw)] = (col, str(raw))
        return out

    def load(self, only=None, redo=False, limit=None):
        wb = self._load_wb()
        try:
            ws = self._ws(wb)
            headers = self._headers(ws)
            orders = []
            for row in range(2, (ws.max_row or 1) + 1):
                raw = {original: ws.cell(row, col).value for col, original in headers.values()}
                order, self.source = _normalise(raw, row)
                if not _is_order(order):
                    continue
                self.rows[str(order["order_id"])] = row
                orders.append(order)
            return _pending(orders, only, redo, limit)
        finally:
            wb.close()

    def _save(self, wb):
        tmp = self.path.with_name(self.path.stem + ".saving.xlsx")
        try:
            wb.save(tmp)
            os.replace(tmp, self.path)
        except PermissionError:
            try:
                tmp.unlink(missing_ok=True)
            except OSError:
                pass
            raise RuntimeError(
                f"Could not save {self.path.name}. Close it in Excel and try again.")
        except Exception:
            try:
                tmp.unlink(missing_ok=True)
            except OSError:
                pass
            raise

    def mark(self, order_id, fields):
        row = self.rows.get(str(order_id))
        if row is None:
            raise RuntimeError(f"Order {order_id} is not in {self.path.name}.")
        shop = shop_columns(fields)
        wb = self._load_wb()
        try:
            ws = self._ws(wb)
            headers = self._headers(ws)
            changed = False
            written = []
            for key, value in shop.items():
                if key not in headers:
                    continue
                col, _ = headers[key]
                cell = ws.cell(row, col)
                if _cell_text(cell.value) != str(value):
                    cell.value = value
                    changed = True
                written.append(f"{key}={value}")
            if changed:
                self._save(wb)
            return written
        finally:
            wb.close()
