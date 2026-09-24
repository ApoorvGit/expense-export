FROM python:3.12-slim

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY main.py .

EXPOSE 8000

# DATABASE_URL and API_KEY are required and have no default — the app
# refuses to start without them (see main.py). Pass them at `docker run`
# time or via docker-compose's env_file; never bake them into the image.
CMD ["uvicorn", "main:app", "--host", "0.0.0.0", "--port", "8000"]
