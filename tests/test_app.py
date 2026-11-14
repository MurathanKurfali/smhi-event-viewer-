import unittest
from pathlib import Path

from streamlit.testing.v1 import AppTest

import db


class AppTests(unittest.TestCase):
    def test_mixed_measurements_are_displayable_without_losing_ranges(self):
        from app import display_frame
        from streamlit import dataframe_util
        import pyarrow as pa

        frame = display_frame([{'Gust Speed (m/s)': 18.5}, {'Gust Speed (m/s)': '18-22'}])
        table = pa.ipc.open_stream(dataframe_util.convert_pandas_df_to_arrow_bytes(frame)).read_all()
        self.assertEqual(table['Gust Speed (m/s)'].to_pylist(), ['18.5', '18-22'])

    def test_filters_empty_selection_pagination_and_cumulative_timeline(self):
        app = AppTest.from_file(str(Path(__file__).resolve().parents[1] / 'app.py'), default_timeout=15).run()
        self.assertEqual(len(app.exception), 0)
        self.assertEqual(app.metric[0].value, f"{db.metadata(db.version())['rows']:,}")
        app.multiselect(key='countries').set_value(['Denmark']).run()
        expected = db.summary(db.version(), db.Filters(countries=('Denmark',)))['total']
        self.assertEqual(app.metric[0].value, f'{expected:,}')
        app.number_input[0].set_value(2).run()
        self.assertEqual(len(app.exception), 0)
        app.multiselect(key='categories').set_value(['Snow']).run()
        self.assertEqual(app.metric[0].value, '1')
        app.radio(key='view_mode').set_value('Time-lapse').run()
        self.assertEqual(len(app.exception), 0)
        latest = max(db.metadata(db.version())['years'])
        app.slider(key='timeline_year').set_value(latest).run()
        app.checkbox(key='cumulative').set_value(True).run()
        self.assertEqual(app.metric[0].value, '1')
        app.multiselect(key='countries').set_value([]).run()
        expected = db.summary(db.version(), db.Filters(categories=('Snow',)))['total']
        self.assertEqual(app.metric[0].value, f'{expected:,}')
        self.assertEqual(len(app.exception), 0)


if __name__ == '__main__':
    unittest.main()
