import csv
import tempfile
import unittest
from pathlib import Path

import outreach_sender


class OutreachSenderSentLogTests(unittest.TestCase):
    def test_load_sent_log_handles_legacy_header_with_current_rows(self):
        with tempfile.TemporaryDirectory() as tmp_dir:
            sent_log = Path(tmp_dir) / "sent_log.csv"
            sent_log.write_text(
                "\n".join(
                    [
                        "phone,name,agency,template_name,template_sid,sent_at,twilio_sid,status,error",
                        "+351111111111,Ana,Agency,remodelar_agentes_outreach,HXlegacy,2026-03-31T10:00:00Z,MMlegacy,sent,",
                        "+351962010734,Florbela San Miguel,Agency,remodelar_agentes_outreach,HXcurrent,A,2026-04-01T08:00:58.169377Z,MMcurrent,sent,",
                    ]
                )
                + "\n",
                encoding="utf-8",
            )

            loaded = outreach_sender.load_sent_log(sent_log)

            self.assertEqual(outreach_sender.count_sent_today(loaded, today="2026-04-01"), 1)
            self.assertEqual(loaded["+351962010734"][0]["variant"], "A")
            self.assertEqual(loaded["+351962010734"][0]["sent_at"], "2026-04-01T08:00:58.169377Z")
            self.assertEqual(loaded["+351962010734"][0]["status"], "sent")

    def test_append_sent_log_rewrites_legacy_header_to_canonical_schema(self):
        with tempfile.TemporaryDirectory() as tmp_dir:
            sent_log = Path(tmp_dir) / "sent_log.csv"
            sent_log.write_text(
                "\n".join(
                    [
                        "phone,name,agency,template_name,template_sid,sent_at,twilio_sid,status,error",
                        "+351111111111,Ana,Agency,remodelar_agentes_outreach,HXlegacy,2026-03-31T10:00:00Z,MMlegacy,sent,",
                    ]
                )
                + "\n",
                encoding="utf-8",
            )

            outreach_sender.append_sent_log(
                {
                    "phone": "+351222222222",
                    "name": "Beatriz",
                    "agency": "Agency",
                    "template_name": "remodelar_conversa_mercado",
                    "template_sid": "HXcurrent",
                    "variant": "A",
                    "sent_at": "2026-04-01T09:00:00Z",
                    "twilio_sid": "MMcurrent",
                    "status": "sent",
                    "error": "",
                },
                sent_log,
            )

            with sent_log.open(newline="") as f:
                rows = list(csv.reader(f))

            self.assertEqual(rows[0], outreach_sender.SENT_LOG_FIELDS)
            self.assertEqual(len(rows[1]), len(outreach_sender.SENT_LOG_FIELDS))
            self.assertEqual(rows[1][5], "")
            self.assertEqual(rows[2][5], "A")


if __name__ == "__main__":
    unittest.main()
