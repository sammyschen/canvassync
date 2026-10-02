"""Cross-platform desktop notifications. Best-effort: failures are logged, never raised."""

from __future__ import annotations

import json
import logging
import os
import subprocess
import sys

log = logging.getLogger("canvassync")

# Windows toast via the WinRT projection available in stock PowerShell 5.1.
# Title/message arrive through env vars to avoid any quoting issues.
_PS_TOAST = """\
$title = $env:CANVASSYNC_TITLE
$message = $env:CANVASSYNC_MESSAGE
[Windows.UI.Notifications.ToastNotificationManager, Windows.UI.Notifications, ContentType = WindowsRuntime] | Out-Null
$template = [Windows.UI.Notifications.ToastNotificationManager]::GetTemplateContent([Windows.UI.Notifications.ToastTemplateType]::ToastText02)
$texts = $template.GetElementsByTagName("text")
$texts.Item(0).AppendChild($template.CreateTextNode($title)) | Out-Null
$texts.Item(1).AppendChild($template.CreateTextNode($message)) | Out-Null
$toast = [Windows.UI.Notifications.ToastNotification]::new($template)
[Windows.UI.Notifications.ToastNotificationManager]::CreateToastNotifier("canvassync").Show($toast)
"""


def notify(title: str, message: str) -> None:
    try:
        if sys.platform == "darwin":
            # json.dumps produces valid AppleScript string literals
            script = f"display notification {json.dumps(message)} with title {json.dumps(title)}"
            subprocess.run(["osascript", "-e", script], capture_output=True, timeout=10)
        elif sys.platform == "win32":
            subprocess.run(
                ["powershell", "-NoProfile", "-NonInteractive", "-Command", _PS_TOAST],
                capture_output=True,
                timeout=15,
                env={**os.environ, "CANVASSYNC_TITLE": title, "CANVASSYNC_MESSAGE": message},
            )
        else:
            subprocess.run(["notify-send", title, message], capture_output=True, timeout=10)
    except Exception as exc:
        log.debug("notification failed: %s", exc)
