FROM python:3.12-alpine

RUN apk add --no-cache openssl

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY bridge.py .

EXPOSE 8443

CMD ["python", "bridge.py"]
