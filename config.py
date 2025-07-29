import os

class Config:
    SECRET_KEY = os.getenv('SECRET_KEY')
    META_TOKEN = os.getenv('META_TOKEN')
    PHONE_NUMBER_ID = os.getenv('PHONE_NUMBER_ID')
    VERIFY_TOKEN = os.getenv('VERIFY_TOKEN')
    DB_PATH = 'database.db'
    SESSION_TIMEOUT = 600

    DB_HOST     = os.getenv('DB_HOST')
    DB_PORT     = int(os.getenv('DB_PORT', 3306))
    DB_USER     = os.getenv('DB_USER')
    DB_PASSWORD = os.getenv('DB_PASSWORD')
    DB_NAME     = os.getenv('DB_NAME')

    UPLOAD_FOLDER = os.getenv('UPLOAD_FOLDER', 'static/uploads')
    MEDIA_FOLDER = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'media')
