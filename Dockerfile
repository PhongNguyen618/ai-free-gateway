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

# Install python dependencies for Gateway & Ecosystem
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Copy all project code including ecosystem agents
COPY . .

# Create storage directories for Gateway SQLite and Airdrop Hunter DB
RUN mkdir -p /app/storage /app/ecosystem/airdrop_hunter/data && \
    chmod -R 777 /app/storage /app/ecosystem/airdrop_hunter/data

# Expose standard container port
EXPOSE 7860

# Start All-in-One Multi-Process Supervisor (Gateway + Airdrop Hunter + Smart Money)
CMD ["python", "ecosystem_supervisor.py"]
