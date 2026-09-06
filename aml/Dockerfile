FROM python:3.11-slim

ENV PYTHONUNBUFFERED=1 PYTHONDONTWRITEBYTECODE=1
WORKDIR /srv

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY app ./app
COPY alembic ./alembic
COPY alembic.ini docker-entrypoint.sh ./

# 以非 root 執行，符合內控手冊 BIC05-02 資訊安全作業之最小權限原則。
RUN chmod +x docker-entrypoint.sh \
 && useradd --system --uid 10001 aml \
 && chown -R aml:aml /srv
USER aml

EXPOSE 8000
HEALTHCHECK --interval=30s --timeout=5s --start-period=20s \
  CMD python -c "import urllib.request,sys; sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:8000/healthz').status==200 else 1)"

CMD ["./docker-entrypoint.sh"]
