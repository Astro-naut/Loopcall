/**
 * Order verifier <-> Google Sheet bridge (free, runs inside Google).
 *
 * Setup (once):
 *   1. Open your orders sheet > Extensions > Apps Script, paste this file.
 *   2. Change TOKEN below to a long random string.
 *   3. Deploy > New deployment > Web app.
 *      Execute as: Me.  Who has access: Anyone.
 *   4. Copy the web app URL (ends in /exec) into SHEET_URL, and TOKEN into
 *      SHEET_TOKEN, on the laptop.
 *
 * The first row must be headers. The app understands common names ("Name",
 * "Phone", "Address", "Order ID", "Product", "<Product> Quantity", "Price",
 * "Flagged Concerns", "Reviewed/Called"); without an order id column, rows are
 * matched by row number. Outcome columns are added automatically.
 */
const TOKEN = 'change-me-to-a-long-random-string';
const SHEET_NAME = '';  // empty = first sheet in the file

// Accepted names for the order id header (lowercase, spaces for underscores).
const ID_HEADERS = ['order id', 'orderid', 'order no', 'order no.', 'order number', 'order #', 'id'];

function norm_(h) {
  return String(h).trim().toLowerCase().replace(/_/g, ' ').replace(/\s+/g, ' ');
}

function sheet_() {
  const book = SpreadsheetApp.getActiveSpreadsheet();
  return SHEET_NAME ? book.getSheetByName(SHEET_NAME) : book.getSheets()[0];
}

function json_(obj) {
  return ContentService.createTextOutput(JSON.stringify(obj))
    .setMimeType(ContentService.MimeType.JSON);
}

function doGet(e) {
  if ((e.parameter || {}).token !== TOKEN) return json_({ error: 'unauthorized' });
  const values = sheet_().getDataRange().getDisplayValues();
  const header = values.shift().map(String);
  const orders = [];
  values.forEach((row, i) => {
    if (!row.some(cell => cell !== '')) return;
    const order = Object.fromEntries(header.map((h, j) => [h, row[j]]));
    order._row = i + 2;  // sheet row number
    orders.push(order);
  });
  return json_({ orders: orders });
}

// If a cell has a dropdown, write the closest allowed option instead of a raw value.
function fitDropdown_(cell, value) {
  const rule = cell.getDataValidation();
  if (!rule || rule.getCriteriaType() !== SpreadsheetApp.DataValidationCriteria.VALUE_IN_LIST) {
    return value;
  }
  const options = rule.getCriteriaValues()[0].map(String);
  if (options.indexOf(value) >= 0) return value;
  const wanted = {
    DONE: /^(done|yes|called|reviewed|completed?)\b/i,
    RETRY: /^(retry|call again|not yet|pending|no)\b/i,
  }[value];
  const match = wanted && options.find(o => wanted.test(o.trim()));
  return match || value;
}

function doPost(e) {
  const body = JSON.parse(e.postData.contents || '{}');
  if (body.token !== TOKEN) return json_({ error: 'unauthorized' });

  const lock = LockService.getScriptLock();
  lock.waitLock(20000);
  try {
    const sh = sheet_();
    const header = sh.getRange(1, 1, 1, sh.getLastColumn()).getValues()[0].map(String);
    let row = Number(body.row) || 0;
    const idCol = header.findIndex(h => ID_HEADERS.indexOf(norm_(h)) >= 0);
    if (idCol >= 0) {
      const ids = sh.getRange(2, idCol + 1, Math.max(sh.getLastRow() - 1, 1), 1)
        .getDisplayValues().map(r => String(r[0]).trim());
      const index = ids.indexOf(String(body.order_id));
      if (index >= 0) row = index + 2;
    }
    if (row < 2) return json_({ error: 'order ' + body.order_id + ' not found' });

    Object.entries(body.fields || {}).forEach(([name, value]) => {
      let col = header.indexOf(name);
      if (col < 0) {
        header.push(name);
        col = header.length - 1;
        sh.getRange(1, col + 1).setValue(name);
      }
      const cell = sh.getRange(row, col + 1);
      cell.setValue(fitDropdown_(cell, String(value)));
    });
    return json_({ ok: true, row: row });
  } finally {
    lock.releaseLock();
  }
}
