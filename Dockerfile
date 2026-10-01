FROM python:3.12-slim
ENV PYTHONUNBUFFERED=1 PYTHONDONTWRITEBYTECODE=1
WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt \
    && useradd --uid 10001 --create-home app \
    && mkdir -p /app/data \
    && chown -R app:app /app
COPY --chown=app:app chatbot ./chatbot
USER app
CMD ["python", "-m", "chatbot.bot"]
