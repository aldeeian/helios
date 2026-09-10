"""Alerting: turn pipeline findings into notifications people actually receive.

The rest of Helios is happy to write JSON to disk, but a variance nobody reads
is a variance nobody acts on. This package evaluates thresholds against the
variance report and the drift report, renders a digest, and fans it out to
whichever channels are configured (file always, plus Slack/email if credentials
are present).

Stdlib only — no new dependency for HTTP or SMTP.
"""

from .channels import Alert, build_senders, send  # noqa: F401
from .rules import Finding, Thresholds, evaluate_drift, evaluate_variance  # noqa: F401
