from aiogram import Bot
from aiogram.client.default import DefaultBotProperties

from config import TG_TOKEN
from config_bd.db_utils import DbReader

bot = Bot(token=TG_TOKEN, default=DefaultBotProperties(parse_mode="HTML"))
sql = DbReader()
