import os

class Config:
    SECRET_KEY = os.getenv('SECRET_KEY')
    META_TOKEN = os.getenv('META_TOKEN')
    PHONE_NUMBER_ID = os.getenv('PHONE_NUMBER_ID')
    VERIFY_TOKEN = os.getenv('VERIFY_TOKEN')
    DB_PATH = 'database.db'
    SESSION_TIMEOUT = 600
