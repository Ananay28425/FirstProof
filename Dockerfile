FROM python:3.12-slim
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1
WORKDIR /srv/firstproof
RUN groupadd --system fp && useradd --system --gid fp --home /srv/firstproof fp
COPY pyproject.toml ./
COPY firstproof ./firstproof
COPY alembic.ini ./
COPY migrations ./migrations
RUN pip install --no-cache-dir . && chown -R fp:fp /srv/firstproof
USER fp
EXPOSE 8000
CMD ["uvicorn", "firstproof.web:app", "--host", "0.0.0.0", "--port", "8000", "--no-access-log", "--limit-concurrency", "200"]
