FROM python:3.12-slim

WORKDIR /app
RUN pip install --no-cache-dir \
    'arxiv>=2.1.3' \
    'python-dotenv>=1.0.0' \
    'langchain>=0.3.20' \
    'langchain-openai>=0.3.9' \
    'requests>=2.32.0' \
    'scrapy>=2.12.0' \
    'tqdm>=4.67.1'

COPY . /app

ENV PORT=8000

EXPOSE 8000

CMD ["python", "-m", "server.app"]
