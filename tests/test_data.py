import csv
import hashlib
import io
import json
import sqlite3
import tempfile
import unittest
from collections import Counter
from contextlib import closing
from datetime import date, datetime, time
from pathlib import Path

from openpyxl import load_workbook

import db
from build_database import build_database, normalize_date, number

ROOT = Path(__file__).resolve().parents[1]


class DataIntegrityTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.revision = db.version()
        cls.meta = db.metadata(cls.revision)

    def test_every_source_cell_and_row_is_preserved(self):
        """Independent reconciliation of every imported value, including nulls and ranges."""
        counts = Counter()
        manifest = json.loads((ROOT / 'data/sources/manifest.json').read_text())
        with closing(db.connect()) as connection:
            for path in sorted((ROOT / 'data/sources').glob('*.xlsx')):
                self.assertEqual(hashlib.sha256(path.read_bytes()).hexdigest(), manifest[path.name]['sha256'])
                workbook = load_workbook(path, read_only=True, data_only=False)
                try:
                    for sheet in workbook:
                        values = sheet.iter_rows(values_only=True)
                        headers = next(values)
                        cursor = iter(connection.execute(
                            'SELECT country, source_row, raw FROM events WHERE source_file = ? AND source_sheet = ? ORDER BY source_row',
                            (path.name, sheet.title)))
                        for index, values_row in enumerate(values, 2):
                            if all(value is None for value in values_row):
                                continue
                            actual = next(cursor)
                            expected = {header: value.isoformat() if isinstance(value, (datetime, date, time)) else value
                                        for header, value in zip(headers, values_row)}
                            self.assertEqual(json.loads(actual['raw']), expected, f'{path.name}:{sheet.title}:{index}')
                            self.assertEqual(actual['source_row'], index)
                            self.assertEqual(actual['country'], manifest[path.name]['country'])
                            counts[actual['country']] += 1
                        self.assertIsNone(next(cursor, None), f'Extra records in {path.name}:{sheet.title}')
                finally:
                    workbook.close()
            actual_counts = dict(connection.execute('SELECT country, count(*) FROM events GROUP BY country'))
            self.assertEqual(dict(counts), actual_counts)
            self.assertEqual(sum(counts.values()), self.meta['rows'])
            self.assertEqual(connection.execute('PRAGMA integrity_check').fetchone()[0], 'ok')

    def test_weighted_map_preserves_all_mappable_events(self):
        for filters in [db.Filters(), db.Filters(countries=('Denmark',)),
                        db.Filters(countries=('Finland',), categories=('Snow',), years=(2004, 2005), include_undated=False),
                        db.Filters(categories=('Wind',), years=(2024, 2025, 2026), include_undated=False)]:
            counts = db.summary(self.revision, filters)
            points = db.map_points(self.revision, filters)
            self.assertEqual(sum(point[3] for point in points), counts['mappable'])
            self.assertEqual(len({tuple(point[:3]) for point in points}), len(points))
            for latitude, longitude, category, count, event_id in points:
                self.assertTrue(-90 <= latitude <= 90 and -180 <= longitude <= 180)
                self.assertGreater(count, 0)
        self.assertLess(self.meta['mappable'], self.meta['rows'])

    def test_partial_dates_are_included_in_year_filters_without_inventing_days(self):
        with closing(db.connect()) as connection:
            records = connection.execute("SELECT id, date, year, raw FROM events WHERE length(date) < 10").fetchall()
            self.assertTrue(records)
            for record in records:
                self.assertEqual(record['date'], json.loads(record['raw'])['Date'])
                self.assertEqual(record['year'], int(record['date'][:4]))
            year = records[0]['year']
            expected = connection.execute('SELECT count(*) FROM events WHERE year = ?', (year,)).fetchone()[0]
        self.assertEqual(db.summary(self.revision, db.Filters(years=(year,), include_undated=False))['total'], expected)

    def test_pagination_reaches_every_matching_record_in_global_order(self):
        filters = db.Filters(countries=('Denmark',))
        records, count, _ = db.table_page(self.revision, filters)
        joined = []
        for page in range(1, (count + 99) // 100 + 1):
            rows, total, actual_page = db.table_page(self.revision, filters, page)
            self.assertEqual(total, count)
            self.assertEqual(actual_page, page)
            joined.extend(rows)
        self.assertEqual(len(joined), count)
        self.assertEqual(len({row['Event ID'] for row in joined}), count)
        self.assertEqual([row['Date'] for row in joined], sorted(row['Date'] for row in joined))
        self.assertTrue(all(row['Country'] == 'Denmark' for row in joined))
        _, _, last = db.table_page(self.revision, filters, 999999)
        self.assertEqual(last, (count + 99) // 100)
        with self.assertRaises(ValueError):
            db.table_page(self.revision, filters, page_size=251)
        with self.assertRaises(KeyError):
            db.table_page(self.revision, filters, sort='date; DROP TABLE events')

    def test_search_is_literal_and_unicode_case_insensitive(self):
        filters = db.Filters()
        lower, count_lower, _ = db.table_page(self.revision, filters, search='nykøbing')
        upper, count_upper, _ = db.table_page(self.revision, filters, search='NYKØBING')
        self.assertGreater(count_lower, 0)
        self.assertEqual(count_lower, count_upper)
        self.assertEqual(lower, upper)
        _, count_injection, _ = db.table_page(self.revision, filters, search="' OR 1=1 --")
        self.assertEqual(count_injection, 0)

    def test_full_export_includes_unmapped_events_and_every_source_column(self):
        exported = db.export_csv(db.Filters()).decode('utf-8-sig')
        reader = csv.DictReader(io.StringIO(exported))
        self.assertTrue(set(self.meta['source_columns']).issubset(reader.fieldnames))
        counts = Counter()
        ranges = []
        for row in reader:
            counts[row['Country']] += 1
            if '-' in row.get('Gust Speed (m/s)', ''):
                ranges.append(row['Gust Speed (m/s)'])
        self.assertEqual(sum(counts.values()), self.meta['rows'])
        self.assertTrue(ranges)

    def test_runtime_connection_cannot_write(self):
        with closing(db.connect()) as connection:
            with self.assertRaises(sqlite3.OperationalError):
                connection.execute("UPDATE events SET country = 'Changed' WHERE id = -1")

    def test_date_and_coordinate_normalization_preserves_precision(self):
        self.assertEqual(normalize_date('2026-09'), '2026-09')
        self.assertEqual(normalize_date('2024'), '2024')
        self.assertEqual(normalize_date(datetime(2024, 2, 29, 12, 30)), '2024-02-29')
        self.assertIsNone(normalize_date('2025-02-29'))
        self.assertIsNone(normalize_date('2026-13'))
        self.assertEqual(number('58,220001'), 58.220001)
        self.assertEqual(number(0), 0)
        self.assertIsNone(number('unresolved'))
        self.assertIsNone(number(float('inf')))

    def test_invalid_source_does_not_replace_a_working_database(self):
        with tempfile.TemporaryDirectory() as temporary:
            directory = Path(temporary)
            output = directory / 'events.sqlite'
            output.write_bytes(b'previous snapshot')
            # A failed checksum must be detected before opening or replacing the output.
            source = ROOT / 'data/sources/denmark.xlsx'
            (directory / 'denmark.xlsx').write_bytes(source.read_bytes())
            (directory / 'manifest.json').write_text(json.dumps({'denmark.xlsx': {'sha256': 'wrong'}}))
            with self.assertRaises(ValueError):
                build_database(directory, output)
            self.assertEqual(output.read_bytes(), b'previous snapshot')


if __name__ == '__main__':
    unittest.main()
