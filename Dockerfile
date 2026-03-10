FROM python:3.11-slim

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .

# Assume the module is named `job_outreach_tool` locally, but if it runs from the root:
ENV PYTHONPATH=/app

# Expose port 8000 for the FastAPI server
EXPOSE 8000

# Start FastAPI using Uvicorn
CMD ["uvicorn", "job_outreach_tool.api.main:app", "--host", "0.0.0.0", "--port", "8000"]
