"""The small context every new module receives instead of reaching into a shared object (section 4, D24)."""

from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime

from app.platform.config import Settings
from app.platform.db import Database
from app.platform.security import ConfigStore


@dataclass(frozen=True)
class Ctx:
    db: Database
    settings: Settings
    clock: Callable[[], datetime]
    configs: ConfigStore
    carrier: Callable[[str], object]
    # Facts settled once per app session, such as a verified audio credential.
    memo: dict = field(default_factory=dict, compare=False)
