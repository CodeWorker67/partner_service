import os
from pathlib import Path
from typing import Set

from dotenv import load_dotenv

load_dotenv()

TG_TOKEN: str = os.environ.get("TG_TOKEN", "")
ADMIN_IDS: Set[int] = {
    int(x) for x in os.environ.get("ADMIN_IDS", "").split(",") if x.strip()
}
_cid = os.environ.get("CHECKER_ID")
CHECKER_ID: int | None = int(_cid) if _cid else None

DATABASE_PATH: Path = Path(os.environ.get("DATABASE_PATH", "/root/database/partner.db"))
