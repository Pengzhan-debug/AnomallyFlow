FROM python:3.12-slim

WORKDIR /app

# CPU wheels keep the image usable on hosts without CUDA. Replace with the
# matching CUDA index and set DETECTOR_DEVICE=cuda when a GPU is available.
COPY detector/requirements.txt /app/detector/requirements.txt
RUN pip install --no-cache-dir --index-url https://download.pytorch.org/whl/cpu torch torchvision \
    && pip install --no-cache-dir -r /app/detector/requirements.txt

COPY detector /app/detector

ENV PYTHONPATH=/app
ENV TRUST_REMOTE_CODE=1
EXPOSE 8080

# One process: the idempotency cache and per-model locks are in-memory.
CMD ["uvicorn", "detector.app:app", "--host", "0.0.0.0", "--port", "8080", "--workers", "1"]
