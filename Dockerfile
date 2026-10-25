# Dockerfile for Hugging Face Spaces (Docker SDK)
FROM python:3.12-slim

# Prevent Python from writing pyc files and buffering stdout/stderr
ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PYTHONIOENCODING=utf-8 \
    PORT=7860 \
    HOST=0.0.0.0 \
    TESSERACT_CMD=tesseract

# Install system dependencies (Tesseract OCR, Poppler utils, graphics libraries)
RUN apt-get update && apt-get install -y --no-install-recommends \
    tesseract-ocr \
    tesseract-ocr-eng \
    poppler-utils \
    libgl1 \
    && rm -rf /var/lib/apt/lists/*

# Set up non-root user with UID 1000 (standard for Hugging Face Spaces)
RUN useradd -m -u 1000 user
ENV HOME=/home/user \
    PATH=/home/user/.local/bin:$PATH

WORKDIR $HOME/app

# Install Python dependencies first for caching layers
COPY --chown=user:user requirements.txt .
RUN pip install --no-cache-dir --upgrade pip && \
    pip install --no-cache-dir -r requirements.txt

# Copy all project source code, data, and samples
COPY --chown=user:user . .

# Ensure data and uploads directories exist and are writable by UID 1000
RUN mkdir -p data data/uploads && \
    chown -R user:user $HOME/app && \
    chmod -R 775 data

USER user

# Expose default Hugging Face Spaces port
EXPOSE 7860

# Launch FastAPI via uvicorn on 0.0.0.0:7860
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "7860"]
