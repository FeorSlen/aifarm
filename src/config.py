import os

RABBITMQ_URL = os.getenv("RABBITMQ_URL", "amqp://aifarm:aifarm@localhost:5672/%2F")
TASK_EXCHANGE = os.getenv("TASK_EXCHANGE", "aifarm.tasks")
WORKER_ID = os.getenv("WORKER_ID", "worker1")
