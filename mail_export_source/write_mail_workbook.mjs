// Excel writer used by export_mail_last24h.py. No network or credentials required.
import fs from 'node:fs/promises';
import path from 'node:path';
import assert from 'node:assert/strict';
import { createRequire } from 'node:module';
import { pathToFileURL } from 'node:url';

const [inputPath, modulesPath] = process.argv.slice(2);
if (!inputPath) throw new Error('Usage: node write_mail_workbook.mjs mapped_rows.json [node_modules]');
const loader = modulesPath
  ? createRequire(path.join(path.resolve(modulesPath), '__mail_export_loader__.cjs'))
  : createRequire(import.meta.url);
const { Workbook, SpreadsheetFile } = await import(pathToFileURL(loader.resolve('@oai/artifact-tool')).href);
const { headers, rows } = JSON.parse(await fs.readFile(inputPath, 'utf8'));
assert.equal(headers.length, 17);
assert.ok(rows.length < 1048576, 'Excel worksheet row limit exceeded');
const outputDir = path.dirname(path.resolve(inputPath));
const workbook = Workbook.create();
const sheet = workbook.worksheets.add('邮件');
sheet.showGridLines = false;
sheet.tabColor = '#24476A';
const expected = [headers];
for (const [index, row] of rows.entries()) {
  assert.equal(row.length, 17);
  expected.push(row.map((value, col) => {
    if (col === 2 && value) {
      const date = new Date(value);
      assert.ok(Number.isFinite(date.getTime()), 'Invalid receivedDateTime');
      // Numeric Excel datetime, displayed explicitly in UTC.
      return date.getTime() / 86400000 + 25569;
    }
    return value;
  }));
}
sheet.getRange(`A1:Q${expected.length}`).values = expected.map(row =>
  row.map(value => typeof value === 'string' && value.startsWith('=') ? "'" + value : value));
const used = sheet.getRange(`A1:Q${expected.length}`);
used.format.font = { name: 'Arial', size: 10, color: '#203040' };
used.format.verticalAlignment = 'top';
used.format.wrapText = true;
const widths = [54, 44, 25, 52, 20, 32, 42, 58, 58, 58, 58, 58, 58, 64, 112, 18, 24];
for (let col = 0; col < 17; col++) {
  const letter = String.fromCharCode(65 + col);
  sheet.getRange(`${letter}1:${letter}${expected.length}`).format.columnWidth = widths[col];
}
if (rows.length) {
  sheet.getRange(`C2:C${expected.length}`).setNumberFormat('yyyy-mm-dd"T"hh:mm:ss"Z"');
  for (let i = 0; i < rows.length; i++) {
    const height = Math.max(54, Math.min(409, 18 * Math.max(...expected[i + 1].map((value, col) =>
      String(value ?? '').split('\n').reduce((sum, line) => sum + Math.max(1, Math.ceil(line.length / widths[col])), 0)))));
    const range = sheet.getRange(`A${i + 2}:Q${i + 2}`);
    range.format.rowHeight = height;
    if (i % 2 === 1) range.format.fill = '#F1F5F9';
  }
  sheet.tables.add(`A1:Q${expected.length}`, true, 'MailLast24Hours');
}
const heading = sheet.getRange('A1:Q1');
heading.format.fill = '#24476A';
heading.format.font = { name: 'Arial', size: 10, bold: true, color: '#FFFFFF' };
heading.format.rowHeight = 100;
heading.format.horizontalAlignment = 'center';
heading.format.verticalAlignment = 'center';
heading.format.borders = { insideVertical: { style: 'thin', color: '#FFFFFF' } };
sheet.freezePanes.freezeRows(1);
workbook.recalculate();
// Exact all-row, all-column comparisons rather than dumping private mail contents.
const actual = sheet.getRange(`A1:Q${expected.length}`).values;
let checked = 0;
for (let row = 0; row < expected.length; row++) {
  for (let col = 0; col < 17; col++) {
    const wanted = expected[row][col];
    const got = actual[row][col] ?? '';
    assert.equal(got, wanted, `Value mismatch at row ${row + 1}, column ${col + 1}`);
    checked++;
  }
}
const formulas = sheet.getRange(`A1:Q${expected.length}`).formulas;
assert.ok(formulas.every(row => row.every(value => !value)), 'Unexpected Excel formulas');
await workbook.inspect({ kind: 'table', range: '邮件!A1:Q2', tableMaxRows: 2, tableMaxCols: 17 });
// The 17-column schema is wide: preview all columns in three readable parts.
for (const [label, range] of [['ids', 'A1:G2'], ['recipients', 'H1:M2'], ['content', 'N1:Q2']]) {
  const preview = await workbook.render({ sheetName: '邮件', range: rows.length ? range : range.replace(/2/g, '1'), scale: 1, format: 'png' });
  await fs.writeFile(path.join(outputDir, `preview_${label}.png`), new Uint8Array(await preview.arrayBuffer()));
}
await (await SpreadsheetFile.exportXlsx(workbook)).save(path.join(outputDir, 'mail_last24h.xlsx'));
await fs.writeFile(path.join(outputDir, 'validation.json'), JSON.stringify({
  data_rows: rows.length, columns: 17, cells_compared: checked,
  formulas: 0,
  full_values_file: 'mapped_rows.json', date_display_timezone: 'UTC',
}, null, 2));
console.log(JSON.stringify({ excel: path.join(outputDir, 'mail_last24h.xlsx'), messages: rows.length }));
