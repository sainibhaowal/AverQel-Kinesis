"""Alerter - sends notifications on nightly run failures or drift."""

from __future__ import annotations

import json
import os
import smtplib
from dataclasses import dataclass
from datetime import datetime
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText

from .drift_detector import DriftReport
from .nightly_runner import NightlyRunResult


@dataclass
class AlertConfig:
    """Configuration for alerting."""

    # Email settings
    smtp_host: str = ""
    smtp_port: int = 587
    smtp_user: str = ""
    smtp_password: str = ""
    alert_email_from: str = ""
    alert_email_to: list[str] = None

    # Webhook settings
    webhook_url: str = ""
    webhook_secret: str = ""

    # Slack settings
    slack_webhook_url: str = ""

    # File output
    alert_log_file: str = "alerts.log"

    def __post_init__(self):
        if self.alert_email_to is None:
            self.alert_email_to = []


class Alerter:
    """Sends alerts for nightly run results."""

    def __init__(self, config: AlertConfig):
        self.config = config

    async def send_alert(
        self,
        result: NightlyRunResult,
        drift_report: DriftReport | None = None,
    ) -> None:
        """Send alert based on nightly run result."""
        # Always log to file
        self._log_alert(result, drift_report)

        # Send email if configured and there are issues
        if self.config.smtp_host and (
            not result.success or (drift_report and drift_report.has_drift)
        ):
            await self._send_email(result, drift_report)

        # Send webhook if configured
        if self.config.webhook_url and (
            not result.success or (drift_report and drift_report.has_drift)
        ):
            await self._send_webhook(result, drift_report)

        # Send Slack if configured
        if self.config.slack_webhook_url and (
            not result.success or (drift_report and drift_report.has_drift)
        ):
            await self._send_slack(result, drift_report)

    def _log_alert(
        self,
        result: NightlyRunResult,
        drift_report: DriftReport | None = None,
    ) -> None:
        """Log alert to file."""
        log_entry = {
            "timestamp": datetime.utcnow().isoformat() + "Z",
            "run_timestamp": result.timestamp,
            "success": result.success,
            "error": result.error,
            "baseline_passed": result.baseline_replay.get("passed", 0),
            "baseline_failed": result.baseline_replay.get("failed", 0),
            "fresh_count": len(result.fresh_scenarios),
            "fresh_passed": sum(1 for r in result.fresh_scenarios if r.get("success", False)),
            "drift_detected": drift_report.has_drift if drift_report else False,
            "drift_issues": drift_report.issues if drift_report else [],
            "drift_warnings": drift_report.warnings if drift_report else [],
        }

        with open(self.config.alert_log_file, "a") as f:
            f.write(json.dumps(log_entry) + "\n")

    async def _send_email(
        self,
        result: NightlyRunResult,
        drift_report: DriftReport | None = None,
    ) -> None:
        """Send email alert."""
        if not self.config.smtp_host or not self.config.alert_email_to:
            return

        subject = self._build_email_subject(result, drift_report)
        body = self._build_email_body(result, drift_report)

        msg = MIMEMultipart()
        msg["From"] = self.config.alert_email_from
        msg["To"] = ", ".join(self.config.alert_email_to)
        msg["Subject"] = subject
        msg.attach(MIMEText(body, "plain"))

        try:
            with smtplib.SMTP(self.config.smtp_host, self.config.smtp_port) as server:
                server.starttls()
                if self.config.smtp_user and self.config.smtp_password:
                    server.login(self.config.smtp_user, self.config.smtp_password)
                server.send_message(msg)
        except Exception as e:
            print(f"Failed to send email alert: {e}")

    async def _send_webhook(
        self,
        result: NightlyRunResult,
        drift_report: DriftReport | None = None,
    ) -> None:
        """Send webhook alert."""
        import httpx

        payload = {
            "timestamp": datetime.utcnow().isoformat() + "Z",
            "run": {
                "timestamp": result.timestamp,
                "success": result.success,
                "model": result.config.model,
                "baseline_passed": result.baseline_replay.get("passed", 0),
                "baseline_failed": result.baseline_replay.get("failed", 0),
                "fresh_count": len(result.fresh_scenarios),
            },
            "drift": drift_report.to_dict() if drift_report else None,
        }

        headers = {"Content-Type": "application/json"}
        if self.config.webhook_secret:
            headers["X-Webhook-Secret"] = self.config.webhook_secret

        try:
            async with httpx.AsyncClient() as client:
                await client.post(
                    self.config.webhook_url, json=payload, headers=headers, timeout=10
                )
        except Exception as e:
            print(f"Failed to send webhook alert: {e}")

    async def _send_slack(
        self,
        result: NightlyRunResult,
        drift_report: DriftReport | None = None,
    ) -> None:
        """Send Slack alert."""
        import httpx

        color = (
            "danger" if not result.success or (drift_report and drift_report.has_drift) else "good"
        )

        fields = [
            {"title": "Model", "value": result.config.model, "short": True},
            {
                "title": "Baseline Passed",
                "value": str(result.baseline_replay.get("passed", 0)),
                "short": True,
            },
            {
                "title": "Baseline Failed",
                "value": str(result.baseline_replay.get("failed", 0)),
                "short": True,
            },
            {"title": "Fresh Scenarios", "value": str(len(result.fresh_scenarios)), "short": True},
        ]

        if drift_report:
            fields.append(
                {
                    "title": "Drift Detected",
                    "value": "Yes" if drift_report.has_drift else "No",
                    "short": True,
                }
            )
            if drift_report.issues:
                fields.append(
                    {"title": "Issues", "value": "\n".join(drift_report.issues[:5]), "short": False}
                )

        payload = {
            "attachments": [
                {
                    "color": color,
                    "title": f"Nightly Tool-Calling Run: {'FAILED' if not result.success else 'PASSED'}",
                    "fields": fields,
                    "footer": "DeepSpace Tool-Calling Tests",
                    "ts": int(datetime.utcnow().timestamp()),
                }
            ]
        }

        try:
            async with httpx.AsyncClient() as client:
                await client.post(self.config.slack_webhook_url, json=payload, timeout=10)
        except Exception as e:
            print(f"Failed to send Slack alert: {e}")

    def _build_email_subject(
        self,
        result: NightlyRunResult,
        drift_report: DriftReport | None = None,
    ) -> str:
        """Build email subject line."""
        status = "FAILED" if not result.success else "PASSED"
        drift_str = " + DRIFT" if drift_report and drift_report.has_drift else ""
        return f"[DeepSpace Nightly] Tool-Calling Run {status}{drift_str} - {result.config.model}"

    def _build_email_body(
        self,
        result: NightlyRunResult,
        drift_report: DriftReport | None = None,
    ) -> str:
        """Build email body."""
        lines = [
            "DeepSpace Nightly Tool-Calling Test Run",
            "=====================================",
            "",
            f"Timestamp: {result.timestamp}",
            f"Model: {result.config.model}",
            f"LM Studio: {result.config.lm_studio_url}",
            f"Duration: {result.total_duration_seconds:.1f}s",
            f"Overall: {'SUCCESS' if result.success else 'FAILED'}",
            "",
            "Baseline Fixture Replay:",
            f"  Total: {result.baseline_replay.get('total', 0)}",
            f"  Passed: {result.baseline_replay.get('passed', 0)}",
            f"  Failed: {result.baseline_replay.get('failed', 0)}",
            f"  Assertions Passed: {result.baseline_replay.get('assertions_passed', 0)}",
            f"  Assertions Failed: {result.baseline_replay.get('assertions_failed', 0)}",
            "",
            "Fresh Scenarios:",
            f"  Generated: {len(result.fresh_scenarios)}",
            f"  Passed: {sum(1 for r in result.fresh_scenarios if r.get('success', False))}",
            "",
        ]

        if drift_report:
            lines.extend(
                [
                    "Drift Detection:",
                    f"  Drift Detected: {'YES' if drift_report.has_drift else 'NO'}",
                    "",
                ]
            )

            if drift_report.issues:
                lines.append("  Issues:")
                for issue in drift_report.issues:
                    lines.append(f"    - {issue}")
                lines.append("")

            if drift_report.warnings:
                lines.append("  Warnings:")
                for warning in drift_report.warnings:
                    lines.append(f"    - {warning}")
                lines.append("")

        if result.error:
            lines.extend(
                [
                    "Error:",
                    f"  {result.error}",
                    "",
                ]
            )

        return "\n".join(lines)


def create_alert_config_from_env() -> AlertConfig:
    """Create alert config from environment variables."""
    return AlertConfig(
        smtp_host=os.getenv("ALERT_SMTP_HOST", ""),
        smtp_port=int(os.getenv("ALERT_SMTP_PORT", "587")),
        smtp_user=os.getenv("ALERT_SMTP_USER", ""),
        smtp_password=os.getenv("ALERT_SMTP_PASSWORD", ""),
        alert_email_from=os.getenv("ALERT_EMAIL_FROM", ""),
        alert_email_to=(
            os.getenv("ALERT_EMAIL_TO", "").split(",") if os.getenv("ALERT_EMAIL_TO") else []
        ),
        webhook_url=os.getenv("ALERT_WEBHOOK_URL", ""),
        webhook_secret=os.getenv("ALERT_WEBHOOK_SECRET", ""),
        slack_webhook_url=os.getenv("ALERT_SLACK_WEBHOOK", ""),
        alert_log_file=os.getenv("ALERT_LOG_FILE", "alerts.log"),
    )
