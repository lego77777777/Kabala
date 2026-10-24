FROM python:3.12-slim
WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY kabala_server ./kabala_server
ENV PORT=8000 KABALA_DB=/data/kabala_server.db
VOLUME /data
EXPOSE 8000
CMD ["python", "-m", "kabala_server"]
