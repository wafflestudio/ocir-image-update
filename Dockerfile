FROM python:3.12-slim AS build

WORKDIR /function

ENV PIP_DISABLE_PIP_VERSION_CHECK=1
ENV PIP_NO_CACHE_DIR=1
ENV PYTHONDONTWRITEBYTECODE=1
ENV PYTHONUNBUFFERED=1

COPY requirements.txt /function/requirements.txt

RUN python -m venv /python \
    && /python/bin/pip install --upgrade pip \
    && /python/bin/pip install -r /function/requirements.txt

COPY func.py /function/func.py
COPY ocir_image_update.py /function/ocir_image_update.py

FROM python:3.12-slim

WORKDIR /function

ENV PYTHONDONTWRITEBYTECODE=1
ENV PYTHONUNBUFFERED=1
ENV PYTHONPATH=/function:/python

COPY --from=build /python /python
COPY --from=build /function /function

RUN groupadd --gid 1000 fn \
    && useradd --uid 1000 --gid fn --create-home --shell /usr/sbin/nologin fn \
    && chmod -R o+rX /python /function

ENTRYPOINT ["/python/bin/fdk", "/function/func.py", "handler"]
