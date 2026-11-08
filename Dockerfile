# A sandbox for running a round: see "Running inside Docker" in ORGANIZER.md.
FROM python:3.14-slim

WORKDIR /app
COPY requirements-bots.txt .
RUN pip install --no-cache-dir --extra-index-url https://download.pytorch.org/whl/cpu -r requirements-bots.txt

COPY . .
RUN pip install --no-cache-dir -e .

ENTRYPOINT ["poker"]
CMD ["--help"]
