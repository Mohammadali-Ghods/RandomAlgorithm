FROM python:3.12-slim

WORKDIR /app
# Stdlib-only app — no pip dependencies to install.
COPY algorithm_a.py orders.py panel_server.py panel.html manual_server.py manual_panel.html mm_server.py ./

# PANEL_APP selects which server to run: panel_server.py (auto bot, default)
# or manual_server.py (hands-on trading terminal, market3).
ENV PANEL_HOST=0.0.0.0 \
    PANEL_PORT=8787 \
    PANEL_APP=panel_server.py \
    PYTHONUNBUFFERED=1

EXPOSE 8787
CMD ["sh", "-c", "python3 $PANEL_APP"]
