// Persistent XLSX writer. Business decisions live in Python, never in cell coordinates.
import fs from 'node:fs/promises';
import path from 'node:path';
import readline from 'node:readline';
import assert from 'node:assert/strict';
import { createRequire } from 'node:module';
import { pathToFileURL } from 'node:url';

const modules = process.argv[2];
if (!modules) throw new Error('Pass the bundled node_modules directory');
const loader = createRequire(path.join(path.resolve(modules), '__onboarding_loader__.cjs'));
const { Workbook, SpreadsheetFile } = await import(pathToFileURL(loader.resolve('@oai/artifact-tool')).href);

function columnName(index) {
  let result = '';
  for (let n = index + 1; n; n = Math.floor((n - 1) / 26)) result = String.fromCharCode(65 + (n - 1) % 26) + result;
  return result;
}

function toCell(value, field) {
  if (value === null || value === undefined) return '';
  if (field === 'receivedDateTime' && value) {
    const time = new Date(value).getTime();
    assert.ok(Number.isFinite(time), 'Invalid receivedDateTime');
    return time / 86400000 + 25569;
  }
  if (field === '最新收件时间' && value) {
    const time = new Date(value).getTime();
    assert.ok(Number.isFinite(time), 'Invalid progress received time');
    return (time + 8 * 3600000) / 86400000 + 25569;
  }
  assert.ok(['string', 'number', 'boolean'].includes(typeof value), 'Unsupported Excel value type');
  assert.ok(typeof value !== 'string' || value.length <= 32767, 'Text exceeds Excel cell limit; no truncation was applied');
  return value;
}

async function writeWorkbook(request) {
  assert.equal(request.tables.length, 2, 'Exactly two tables are required');
  const workbook = Workbook.create();
  for (const [tableIndex, table] of request.tables.entries()) {
    const sheet = workbook.worksheets.add(table.name);
    const headers = [...table.headers, 'record_id'];
    assert.equal(headers.length, new Set(headers).size, 'Duplicate field names');
    const rows = table.records.map(record => headers.map(h => toCell(h === 'record_id' ? record.record_id : record.fields[h], h)));
    const matrix = [headers, ...rows];
    assert.ok(matrix.length <= 1048576, 'Excel row limit exceeded');
    const end = columnName(headers.length - 1);
    const used = sheet.getRange(`A1:${end}${matrix.length}`);
    used.values = matrix.map(row => row.map(v => typeof v === 'string' && v.startsWith('=') ? "'" + v : v));
    used.format.font = { name: 'Arial', size: 10, color: '#203040' };
    used.format.verticalAlignment = 'top';
    used.format.wrapText = true;
    sheet.showGridLines = false;
    sheet.tabColor = tableIndex === 0 ? '#24476A' : '#7596B4';
    const widths = tableIndex === 0
      ? { '公司名称': 35, '客户名称': 25, '邮件': 42, '球在谁方': 16, '卡点': 65, '简要描述': 74, '经办人': 25, '最新收件时间': 25 }
      : { 'subject': 60, 'body.content（去除html）': 105, '处理说明': 62, 'AI处理': 18, '是否为开户邮件': 20 };
    for (const [i, header] of headers.entries()) {
      const letter = columnName(i);
      let width = widths[header] ?? (header === 'receivedDateTime' ? 27 : header === 'record_id' ? 38 : 40);
      sheet.getRange(`${letter}1:${letter}${matrix.length}`).format.columnWidth = width;
      if (matrix.length > 1 && ['receivedDateTime', '最新收件时间'].includes(header)) {
        sheet.getRange(`${letter}2:${letter}${matrix.length}`).setNumberFormat(
          header === 'receivedDateTime' ? 'yyyy-mm-dd"T"hh:mm:ss"Z"' : 'yyyy-mm-dd hh:mm:ss');
      }
    }
    if (rows.length) {
      sheet.tables.add(`A1:${end}${matrix.length}`, true, tableIndex === 0 ? 'OnboardingProgress' : 'MailInformation');
      for (let i = 0; i < rows.length; i++) {
        const row = sheet.getRange(`A${i + 2}:${end}${i + 2}`);
        // Progress is fully readable. Raw long messages remain intact in the cell/formula bar.
        const longest = Math.max(...rows[i].map((v, col) => {
          const width = widths[headers[col]] ?? 40;
          return String(v).split('\n').reduce((sum, line) => sum + Math.max(1, Math.ceil([...line].reduce((n, c) => n + (c.charCodeAt(0) > 255 ? 2 : 1), 0) / width)), 0);
        }));
        row.format.rowHeight = Math.max(48, Math.min(tableIndex === 0 ? 409 : 96, longest * 15 + 12));
      }
      if (tableIndex === 0) {
        const ball = columnName(headers.indexOf('球在谁方'));
        for (const [status, color] of [['球在我方', '#FFF0C2'], ['球在客户', '#E6F1FF'], ['球在合规', '#EEE8FA']]) {
          sheet.getRange(`${ball}2:${ball}${matrix.length}`).conditionalFormats.add('containsText', { text: status, format: { fill: color } });
        }
      }
    }
    const heading = sheet.getRange(`A1:${end}1`);
    heading.format.fill = '#24476A';
    heading.format.font = { name: 'Arial', size: 10, bold: true, color: '#FFFFFF' };
    heading.format.horizontalAlignment = 'center';
    heading.format.verticalAlignment = 'center';
    heading.format.rowHeight = 42;
    heading.format.borders = { insideVertical: { style: 'thin', color: '#FFFFFF' } };
    sheet.freezePanes.freezeRows(1);
    const actual = used.values;
    for (let r = 0; r < matrix.length; r++) {
      for (let c = 0; c < headers.length; c++) assert.equal(actual[r][c] ?? '', matrix[r][c], `Excel value mismatch: ${tableIndex}/${r}/${c}`);
    }
    assert.ok(used.formulas.every(row => row.every(v => !v)), 'Unexpected Excel formulas');
  }
  workbook.recalculate();
  await workbook.inspect({ kind: 'sheet', include: 'id,name', maxChars: 1200 });
  if (request.preview_dir) {
    await fs.mkdir(request.preview_dir, { recursive: true });
    const previews = [
      ['progress', '开户进度表', 'A1:D4'], ['progress_details', '开户进度表', 'D1:H4'],
      ['mail', '邮件信息表', 'N1:R4'],
    ];
    for (const [label, sheetName, range] of previews) {
      const blob = await workbook.render({ sheetName, range, scale: 1.25, format: 'png' });
      await fs.writeFile(path.join(request.preview_dir, `${label}.png`), new Uint8Array(await blob.arrayBuffer()));
    }
  }
  await fs.mkdir(path.dirname(request.path), { recursive: true });
  const temporary = request.path + '.writing.xlsx';
  try {
    await (await SpreadsheetFile.exportXlsx(workbook)).save(temporary);
    await fs.rename(temporary, request.path);
  } catch (error) {
    await fs.rm(temporary, { force: true }).catch(() => {});
    if (['EPERM', 'EACCES', 'EBUSY'].includes(error.code)) throw new Error('Close the workbook in Excel and retry');
    throw error;
  }
}

const lines = readline.createInterface({ input: process.stdin, crlfDelay: Infinity });
for await (const line of lines) {
  try {
    await writeWorkbook(JSON.parse(line));
    process.stdout.write('@@ONBOARDING@@' + JSON.stringify({ ok: true }) + '\n');
  } catch (error) {
    process.stdout.write('@@ONBOARDING@@' + JSON.stringify({ ok: false, error: error.message }) + '\n');
  }
}
