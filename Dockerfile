FROM python:3.10-slim

WORKDIR /app

COPY server.py /app/
COPY public /app/public

ENV PORT=8088
ENV HOST=0.0.0.0

EXPOSE 8088

CMD ["python3", "server.py"]
