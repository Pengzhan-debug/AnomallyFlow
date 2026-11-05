FROM python:3.12-slim

WORKDIR /app
COPY backend/requirements.txt /app/backend/requirements.txt
RUN pip install --no-cache-dir -r /app/backend/requirements.txt
COPY alembic.ini /app/alembic.ini
COPY backend /app/backend
COPY data /app/data
ENV PYTHONPATH=/app/backend
ENV STORAGE_ROOT=/app/storage
RUN chmod +x /app/backend/entrypoint.sh
EXPOSE 8000
CMD ["/app/backend/entrypoint.sh"]

