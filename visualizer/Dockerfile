FROM python:3.13-slim

WORKDIR /app

COPY . /app

EXPOSE 3000

CMD ["sh", "-c", "python -m http.server ${PORT:-3000}"]
