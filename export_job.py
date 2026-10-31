"""Cloud Run Job entrypoint: refreshes the dashboard once, then exits so nothing runs (or bills) between runs."""
import logging

import main

if __name__ == "__main__":
    if not main.SPREADSHEET_ID:
        raise SystemExit("SPREADSHEET_ID not set; nothing to export.")
    try:
        main.ensure_mismatch_highlighting()
    except Exception:
        logging.getLogger("checkin").exception("Could not set up mismatch highlighting.")
    main.run_full_export()
