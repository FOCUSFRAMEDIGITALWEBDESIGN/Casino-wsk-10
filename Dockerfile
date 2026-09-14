FROM python:3.12-slim
WORKDIR /app
COPY bot.py launch.py /app/
COPY tests /app/tests
RUN python -m unittest discover -s tests -v
ENV PYTHONUNBUFFERED=1 DATA_DIR=/data TRADING_MODE=paper
CMD ["python", "-u", "launch.py", "run"]
