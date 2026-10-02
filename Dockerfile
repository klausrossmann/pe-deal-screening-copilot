FROM python:3.11-slim

WORKDIR /app

COPY requirements.txt .
# CPU-only torch first: sentence-transformers would otherwise pull the much larger CUDA build.
RUN pip install --no-cache-dir torch --index-url https://download.pytorch.org/whl/cpu
RUN pip install --no-cache-dir -r requirements.txt

COPY . .

EXPOSE 8000 8501

CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]
