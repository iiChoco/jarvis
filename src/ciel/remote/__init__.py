"""Remote text lanes — ways to reach Ciel from outside the terminal.

Two transports today — Discord (:mod:`ciel.remote.discord`) for away, the
web GUI (:mod:`ciel.remote.web`) for at-the-machine-but-quiet — but the
package boundary is the point: the pipeline talks to a small
queue-and-send surface (written down as :class:`ciel.remote.lane.Lane`),
never to a chat library or an HTTP framework, so a future transport — or
the hub itself — slots in without touching anything upstream.
"""

from ciel.remote.discord import DiscordLink, RemoteUnavailable
from ciel.remote.lane import Lane
from ciel.remote.web import WebIndicator, WebLink

__all__ = ["DiscordLink", "Lane", "RemoteUnavailable", "WebLink", "WebIndicator"]
