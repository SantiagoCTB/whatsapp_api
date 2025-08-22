FROM python:3.11-slim

# Install system dependencies
RUN apt-get update && apt-get install -y ffmpeg && rm -rf /var/lib/apt/lists/*

# Set work directory
WORKDIR /app

# Install dependencies
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Copy project code
COPY . /app

# Expose ports for Flask and Streamlit
EXPOSE 5000 8501

# Copy and set permissions for start script
COPY start.sh /start.sh
RUN chmod +x /start.sh

# Default command
CMD ["/start.sh"]
