FROM python:3.12-slim
WORKDIR /app
COPY bot.py launch.py discord_gateway.py requirements.txt /app/
COPY tests /app/tests
RUN pip install --no-cache-dir -r requirements.txt
ENV PYTHONUNBUFFERED=1 DATA_DIR=/data TRADING_MODE=paper
CMD ["python", "-u", "launch.py", "run"]
