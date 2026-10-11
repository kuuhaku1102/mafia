"""JST conversion and Sheets timestamp writes, without network or credentials."""
import unittest
from datetime import datetime, timezone
from unittest.mock import Mock, patch

from test_parse import scrape


class SheetsTimestampTest(unittest.TestCase):
    def test_write_uses_jst_at_utc_date_boundary(self):
        import gspread
        from google.oauth2.service_account import Credentials

        client = Mock()
        ws = client.open_by_key.return_value.worksheet.return_value
        item = {key: "sample" for key in scrape.HEADERS[:-1]}
        instant = datetime(2026, 10, 10, 15, 0, 1, tzinfo=timezone.utc)
        with (
            patch.dict(scrape.os.environ, {"SPREADSHEET_ID": "test", "WORKSHEET_NAME": "bank"}),
            patch.object(scrape, "_load_service_account_info", return_value={}),
            patch.object(Credentials, "from_service_account_info"),
            patch.object(gspread, "authorize", return_value=client),
            patch.object(scrape, "datetime") as clock,
        ):
            clock.now.side_effect = lambda tz: instant.astimezone(tz)
            scrape.write_to_sheets([item, item])

        values = ws.update.call_args.kwargs["values"]
        self.assertEqual(values[0], scrape.HEADERS)
        self.assertEqual([row[6] for row in values[1:]], ["2026-10-11 00:00:01"] * 2)
        self.assertEqual(ws.update.call_args.kwargs["range_name"], "A1:G3")
        self.assertEqual(ws.update.call_args.kwargs["value_input_option"], "USER_ENTERED")
        ws.format.assert_called_once_with("G2:G3", {
            "numberFormat": {"type": "DATE_TIME", "pattern": "yyyy-mm-dd hh:mm:ss"},
        })

    def test_dry_run_never_writes_to_sheets(self):
        with (
            patch.dict(scrape.os.environ, {"DRY_RUN": "1"}),
            patch.object(scrape, "scrape_all", return_value=[{"商品名": "sample"}]),
            patch.object(scrape, "write_to_sheets") as write,
        ):
            self.assertEqual(scrape.main(), 0)
            write.assert_not_called()


if __name__ == "__main__":
    unittest.main()
