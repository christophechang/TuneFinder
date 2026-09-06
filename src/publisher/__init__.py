"""The pool publisher: posts a day's corpus to the multi-tenant API.

Nothing in here runs during the Sunday report; the publisher is a separate
command with its own settings file and its own snapshots.
"""

# Sent with every batch as `publisher_version` so the API can tell which build
# produced a document. Bumped by hand when the publisher's wire behaviour changes
# (TuneFinder has no version constant in code — the release number lives in
# CHANGELOG.md — and the wire version is not the release version anyway).
PUBLISHER_VERSION = "1"
