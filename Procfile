web: bash scripts/build_frontend.sh && gunicorn app:app --bind 0.0.0.0:$PORT --workers 4
streamlit: streamlit run scripts/tablero.py --server.address 0.0.0.0 --server.enableCORS=false --server.enableXsrfProtection=false
