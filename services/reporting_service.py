import os
import json
import datetime
import logging
from typing import Dict, Any, List, Optional
from flask import session

from utils.log_sanitizer import sanitize_log_value

logger = logging.getLogger(__name__)


class ReportingService:
    REPORTS_FILE = "user_reports.json"

    @staticmethod
    def report_recipe(filename: str, reason: str, user_id: Optional[str] = None) -> Dict[str, str]:
        """
        Log a user report about a recipe or image.

        Args:
            filename: The filename of the recipe being reported.
            reason: The reason for reporting.
            user_id: The ID of the user reporting (defaults to session user_id or 'anonymous').

        Returns:
            Dict[str, str]: Success message.
        """
        try:
            # Sanitize reason input - limit length
            reason = reason[:500]

            report_entry = {
                "timestamp": datetime.datetime.now().isoformat(),
                "filename": filename,
                "reason": reason,
                "user_id": user_id or session.get("user_id", "anonymous"),
            }

            reports = ReportingService._get_all_reports()
            reports.append(report_entry)

            with open(ReportingService.REPORTS_FILE, "w") as f:
                json.dump(reports, f, indent=2)

            logger.info(
                "Report submitted for %s by %s",
                sanitize_log_value(filename),
                sanitize_log_value(report_entry["user_id"]),
            )
            return {"message": "Report submitted successfully"}

        except Exception as e:
            logger.error(
                "Error logging report for %s: %s",
                sanitize_log_value(filename),
                sanitize_log_value(e),
            )
            raise

    @staticmethod
    def _get_all_reports() -> List[Dict[str, Any]]:
        """Helper to load all reports from file."""
        if not os.path.exists(ReportingService.REPORTS_FILE):
            return []

        try:
            with open(ReportingService.REPORTS_FILE, "r") as f:
                return json.load(f)  # type: ignore[no-any-return]
        except (json.JSONDecodeError, IOError) as e:
            logger.warning(f"Could not load reports file: {e}. Starting fresh.")
            return []
