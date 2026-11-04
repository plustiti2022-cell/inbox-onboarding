"""Independently verify the saved XLSX against mapped_rows.json. No API calls."""
import json
import sys
import zipfile
import xml.etree.ElementTree as ET
from datetime import datetime
from pathlib import Path


def verify(directory):
    directory = Path(directory)
    data = json.loads((directory / 'mapped_rows.json').read_text(encoding='utf-8'))
    assert len(data['headers']) == 17 and data['headers'][-2:] == ['AI处理', '是否为开户邮件']
    assert all(len(row) == 17 and row[-2:] == ['AI未完成', ''] for row in data['rows'])
    times = [datetime.fromisoformat(row[2].replace('Z', '+00:00')) for row in data['rows']]
    assert times == sorted(times), 'Mail must be exported oldest first'
    manifest = json.loads((directory / 'run_manifest.json').read_text(encoding='utf-8'))
    namespace = {'s': 'http://schemas.openxmlformats.org/spreadsheetml/2006/main'}
    with zipfile.ZipFile(directory / 'mail_last24h.xlsx') as archive:
        shared = []
        if 'xl/sharedStrings.xml' in archive.namelist():
            shared = [''.join(node.itertext()) for node in
                      ET.fromstring(archive.read('xl/sharedStrings.xml')).findall('s:si', namespace)]
        sheet = ET.fromstring(archive.read('xl/worksheets/sheet1.xml'))
        cells = {cell.attrib['r']: cell for cell in
                 sheet.findall('.//s:sheetData/s:row/s:c', namespace)}
        expected = [data['headers']] + data['rows']
        count = 0
        for row_number, row in enumerate(expected, 1):
            for column, wanted in enumerate(row):
                address = chr(65 + column) + str(row_number)
                cell = cells.get(address)
                if cell is None:
                    assert wanted == '', address
                    continue
                assert cell.find('s:f', namespace) is None, address
                kind = cell.get('t')
                node = cell.find('s:v', namespace)
                raw = node.text if node is not None else ''
                if kind == 's':
                    actual = shared[int(raw)]
                elif kind == 'inlineStr':
                    actual = ''.join(cell.find('s:is', namespace).itertext())
                else:
                    actual = raw
                if row_number > 1 and column == 2:
                    timestamp = datetime.fromisoformat(wanted.replace('Z', '+00:00'))
                    assert abs(float(actual) - (timestamp.timestamp() / 86400 + 25569)) < 1e-8, address
                    start = datetime.fromisoformat(manifest['window_start_utc_inclusive'].replace('Z', '+00:00'))
                    end = datetime.fromisoformat(manifest['window_end_utc_exclusive'].replace('Z', '+00:00'))
                    assert start <= timestamp < end, address
                elif isinstance(wanted, bool):
                    assert kind == 'b' and actual == str(int(wanted)), address
                else:
                    assert actual == wanted, address
                count += 1
        assert sheet.find('.//s:pane', namespace).get('ySplit') == '1'
        if data['rows']:
            table = ET.fromstring(archive.read('xl/tables/table1.xml'))
            assert table.get('ref') == f'A1:Q{len(expected)}'
            assert [column.get('name') for column in table.findall('s:tableColumns/s:tableColumn', namespace)] == data['headers']
    result = {'saved_xlsx_verified': True, 'data_rows': len(data['rows']),
              'columns': 17, 'populated_cells_compared': count,
              'ai_status_column': 'PASS',
              'account_opening_column_blank': 'PASS',
              'headers_values_utc_dates_booleans_time_window_freeze_filter': 'PASS'}
    (directory / 'saved_file_verification.json').write_text(json.dumps(result, indent=2), encoding='utf-8')
    print(json.dumps(result))


if __name__ == '__main__':
    verify(sys.argv[1])
