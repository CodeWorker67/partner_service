import logging
from pathlib import Path

from loguru import logger

Path("logs").mkdir(exist_ok=True)

time_format = "{time:YYYY-MM-DD!UTC}"
logger.add(
    f"logs/{time_format}_log.log",
    format="[{level}]\t[{time}] [{file}]\t: {message}",
    rotation="00:00",
)
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(levelname)s - %(name)s - %(message)s",
)
