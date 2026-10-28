/**
 * Limassol Seller → Google Sheet. Paste into the sheet: Extensions → Apps Script, replace everything with this.
 * Set SECRET below to the same value as SHEETS_SECRET on Railway, then Deploy → New deployment → Web app:
 *   Execute as: Me · Who has access: Anyone. Copy the web app URL into SHEETS_WEBHOOK_URL on Railway.
 *
 * The server sends rows; this script creates the two sheets if needed and updates rows by their id
 * (an inquiry/buyer that changes is updated in place, never duplicated).
 */
const SECRET = 'ВСТАВЬ_СЮДА_SHEETS_SECRET';

const SHEETS = {
  inquiries: {
    name: 'Отклики', key: 'ID отклика',
    headers: ['ID отклика', 'Дата', 'Покупатель', 'Username', 'Telegram ID', 'Вещь', 'Цена', 'Минимум', 'Предложил',
              'Намерение', 'Суть', 'Сообщение', 'Где', 'Ссылка на чат', 'Статус вещи'],
  },
  buyers: {
    name: 'Покупатели', key: 'Telegram ID',
    headers: ['Telegram ID', 'Имя', 'Username', 'Ссылка на чат', 'Первое сообщение', 'Последнее сообщение',
              'Откликов', 'Интересовался', 'Купил', 'Заметки'],
  },
};

function doPost(e) {
  let data;
  try {
    data = JSON.parse(e.postData.contents);
  } catch (err) {
    return reply({ ok: false, error: 'bad json' });
  }
  if (data.secret !== SECRET) return reply({ ok: false, error: 'bad secret' });
  const lock = LockService.getScriptLock();
  lock.waitLock(30000);
  try {
    const ss = SpreadsheetApp.getActiveSpreadsheet();
    upsertAll(ss, SHEETS.inquiries, data.inquiries || []);
    upsertAll(ss, SHEETS.buyers, data.buyers || []);
  } finally {
    lock.releaseLock();
  }
  return reply({ ok: true });
}

function doGet() {
  return reply({ ok: true, info: 'Limassol Seller sheet endpoint' });
}

function upsertAll(ss, spec, rows) {
  if (!rows.length) return;
  let sh = ss.getSheetByName(spec.name);
  if (!sh) {
    sh = ss.insertSheet(spec.name);
    sh.getRange(1, 1, 1, spec.headers.length).setValues([spec.headers]).setFontWeight('bold');
    sh.setFrozenRows(1);
  }
  const lastCol = Math.max(sh.getLastColumn(), spec.headers.length);
  const headers = sh.getRange(1, 1, 1, lastCol).getValues()[0];
  const keyCol = headers.indexOf(spec.key);
  const lastRow = sh.getLastRow();
  const keys = lastRow > 1 ? sh.getRange(2, keyCol + 1, lastRow - 1, 1).getValues().map(r => norm(r[0])) : [];
  const existing = keys.length;
  const appended = [];
  rows.forEach(row => {
    const at = keys.indexOf(norm(row[spec.key]));
    if (at >= existing) {  // the same id twice in one batch: update the row we are about to append
      headers.forEach((h, i) => { if (h in row) appended[at - existing][i] = row[h]; });
    } else if (at >= 0) {
      // Update only the columns the server knows: your own columns (like «Заметки») are never overwritten.
      const range = sh.getRange(at + 2, 1, 1, lastCol);
      const values = range.getValues()[0];
      headers.forEach((h, i) => { if (h in row) values[i] = row[h]; });
      range.setValues([values]);
    } else {
      appended.push(headers.map(h => (h in row ? row[h] : '')));
      keys.push(norm(row[spec.key]));
    }
  });
  if (appended.length) sh.getRange(sh.getLastRow() + 1, 1, appended.length, lastCol).setValues(appended);
}

// "'123" (forced text) and 123 are the same id.
function norm(v) {
  return String(v).replace(/^'/, '');
}

function reply(obj) {
  return ContentService.createTextOutput(JSON.stringify(obj)).setMimeType(ContentService.MimeType.JSON);
}
