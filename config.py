import os

class Config:
    SECRET_KEY = os.getenv('SECRET_KEY')
    META_TOKEN = os.getenv('META_TOKEN')
    PHONE_NUMBER_ID = os.getenv('PHONE_NUMBER_ID')
    VERIFY_TOKEN = os.getenv('VERIFY_TOKEN')
    DB_PATH = 'database.db'
    SESSION_TIMEOUT = 600
    STREAMLIT_URL = os.getenv("STREAMLIT_URL", "http://localhost:8501")

    MAX_TRANSCRIPTION_DURATION_MS = int(os.getenv('MAX_TRANSCRIPTION_DURATION_MS', 60000))
    TRANSCRIPTION_MAX_AVG_TIME_SEC = float(os.getenv('TRANSCRIPTION_MAX_AVG_TIME_SEC', 10))

    DB_HOST     = os.getenv('DB_HOST')
    DB_PORT     = int(os.getenv('DB_PORT', 3306))
    DB_USER     = os.getenv('DB_USER')
    DB_PASSWORD = os.getenv('DB_PASSWORD')
    DB_NAME     = os.getenv('DB_NAME')

    BASEDIR       = os.path.dirname(os.path.abspath(__file__))
    UPLOAD_FOLDER = os.path.join(BASEDIR, 'static', 'uploads')
