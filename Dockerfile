FROM python:3.11-slim

# Set working directory
WORKDIR /app

# Set environment defaults:
# - Binds to 0.0.0.0
# - Hugging Face default port 7860 (or dynamic $PORT for Render/Railway/Koyeb)
# - Permissive host and CORS headers for public API consumption
ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PORT=7860 \
    GATEWAY_HOST=0.0.0.0 \
    ALLOW_INSECURE_EXTERNAL=true \
    ALLOWED_HOSTS=* \
    CORS_ALLOWED_ORIGINS=*

# Install python dependencies
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Copy all project code
COPY . .

# Create storage directory for SQLite database with write permissions
RUN mkdir -p /app/storage && chmod -R 777 /app/storage

# Expose standard container port
EXPOSE 7860

# Start server using dynamic $PORT (compatible with Hugging Face, Render, Railway, Koyeb)
CMD ["sh", "-c", "uvicorn src.main:app --host 0.0.0.0 --port ${PORT:-7860}"]
