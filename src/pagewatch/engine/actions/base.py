"""What every action receives: a plain description of one detected change."""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from pagewatch.engine.core import Engine


@dataclass(slots=True)
class AlertContext:
    bookmark_id: int
    name: str
    url: str
    info: tuple[str | None, str | None, str | None]
    change_id: int
    detected_at: str
    summary: str | None
    added_words: int | None
    removed_words: int | None
    changed_blocks: int | None
    checks_accumulated: int
    keyword_hits: list[str]
    private: bool  # alert_privacy: private -> never include page content
    params: dict[str, Any] = field(default_factory=dict)

    @property
    def title(self) -> str:
        return self.name

    @property
    def body(self) -> str:
        """Notification text. Private alerts say only which bookmark changed."""
        if self.private:
            return "Changed. Open PageWatch to review."
        text = self.summary or "The page changed."
        if self.checks_accumulated > 1:
            text = f"(over {self.checks_accumulated} checks) {text}"
        return text


Action = Callable[["Engine", AlertContext], Awaitable[None]]


class ActionUnavailable(RuntimeError):
    """The action type exists in the model but is not implemented in this build; the job is
    marked failed immediately (no retries) so it shows up in Problems."""
