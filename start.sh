#!/bin/sh
set -e

PORT="${PORT:-5000}"
gunicorn app:app --bind 0.0.0.0:$PORT --workers 4 &
streamlit run scripts/tablero.py --server.address 0.0.0.0 --server.enableCORS=false --server.enableXsrfProtection=false
