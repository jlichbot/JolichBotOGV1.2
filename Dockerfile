FROM python:3.11-slim

# Set working directory
WORKDIR /app

# Install dependencies
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Copy project files
COPY . .

# Install sitecustomize so price_fallback patch auto-applies to every process
RUN cp sitecustomize.py $(python -c "import site; print(site.getsitepackages()[0])")/sitecustomize.py

# Create writable directory for daily_spend.json
RUN mkdir -p /data && chmod 777 /data

# Default command (overridden by Railway cron)
CMD ["python", "run.py"]
