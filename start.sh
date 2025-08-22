#!/bin/sh
set -e

gunicorn app:app --bind 0.0.0.0:5000 --workers 4 &
streamlit run scripts/tablero.py --server.address 0.0.0.0 --server.enableCORS=false --server.enableXsrfProtection=false
