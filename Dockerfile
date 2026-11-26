FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1

WORKDIR /app

COPY requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt

COPY app ./app
COPY wsgi.py ./

EXPOSE 5000

HEALTHCHECK --interval=5s --timeout=3s --retries=12 \
  CMD python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:5000/healthz', timeout=2).read()" || exit 1

CMD ["waitress-serve", "--host=0.0.0.0", "--port=5000", "--threads=8", "wsgi:app"]
