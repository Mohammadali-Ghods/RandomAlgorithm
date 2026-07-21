FROM python:3.12-slim

WORKDIR /app
# Stdlib-only app — no pip dependencies to install.
COPY algorithm_a.py orders.py panel_server.py panel.html ./

ENV PANEL_HOST=0.0.0.0 \
    PANEL_PORT=8787 \
    PYTHONUNBUFFERED=1

EXPOSE 8787
CMD ["python3", "panel_server.py"]
