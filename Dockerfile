# IPO Drafting Assistant: Streamlit app with the LangGraph agent (page 6). One container, one process.
# Build:  docker build -t ipo-drafting .
# Run:    docker run --rm -p 8501:8501 --env-file .env -e APP_PASSWORD=... ipo-drafting
# Health: http://localhost:8501/_stcore/health
FROM python:3.11-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    # deals and the text cache live here; on Render free this disk is wiped when the app sleeps
    IPO_DATA_DIR=/tmp/ipo/data \
    # the token-count file is baked into the image, so counting tokens needs no download at run time
    TIKTOKEN_CACHE_DIR=/opt/tiktoken \
    # Ragas must not send usage data (the agent also forces this)
    RAGAS_DO_NOT_TRACK=true \
    # local default; Render sets PORT itself
    PORT=8501

WORKDIR /app

# exact, tested versions (a fresh resolve of the ranged lists can backtrack for a very long time)
COPY requirements-lock.txt ./
RUN pip install -r requirements-lock.txt \
 && python -c "import tiktoken; tiktoken.get_encoding('o200k_base')"

COPY app.py ./
COPY core/ core/
COPY agent/ agent/
COPY pages/ pages/
COPY config/ config/
COPY assets/ assets/

RUN useradd --create-home --uid 10001 app \
 && mkdir -p /tmp/ipo/data \
 && chown -R app:app /tmp/ipo
USER app

EXPOSE 8501
HEALTHCHECK --interval=30s --timeout=5s --start-period=60s --retries=3 \
  CMD python -c "import os, urllib.request; urllib.request.urlopen('http://127.0.0.1:%s/_stcore/health' % os.environ['PORT'], timeout=4)"

CMD ["sh", "-c", "exec streamlit run app.py --server.port \"$PORT\" --server.address 0.0.0.0 --server.headless true --browser.gatherUsageStats false --server.fileWatcherType none"]
