"""iMessage access: reading the Messages database, resolving contacts, sending.

Split from the tool layer for the same reason memory is: the mechanics of
talking to macOS have nothing to do with how Ciel decides to use them, and the
mechanics are the part most likely to break under an OS update.
"""

from __future__ import annotations

from ciel.messages.imessage import (
    Contact,
    Message,
    MessagesClient,
    MessagesUnavailable,
)

__all__ = ["Contact", "Message", "MessagesClient", "MessagesUnavailable"]
