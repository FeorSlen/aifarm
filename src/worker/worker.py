import logging
import time

import pika

from .browser import BlockedError, Browser, ProfilePoolExhaustedError
from src.config import RABBITMQ_URL, TASK_EXCHANGE, WORKER_ID
from src.contract import Result, Task

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger(__name__)

QUEUE = f"{WORKER_ID}.tasks"


def handle(browser: Browser, task: Task) -> Result:
    session_id = task.session_id
    log.info("task session=%s query=%r", session_id or "new", (task.query or "")[:60])
    try:
        if task.query is None:
            if session_id is None:
                return Result(sessions=browser.list_sessions(task.user_session_id))
            browser.close_session(task.user_session_id, session_id)
            return Result(session_id=session_id)
        if session_id is None:
            session_id = browser.new_search_session(task.user_session_id, task.query)
        else:
            browser.ask_followup(task.user_session_id, session_id, task.query)
        return Result(
            session_id=session_id,
            answer=browser.get_last_ai_answer(task.user_session_id, session_id),
        )
    except BlockedError as error:
        log.warning("blocked: %s", error)
        sessions = browser.list_sessions(task.user_session_id)
        profile_id, next_profile_id = browser.burn_and_rotate()
        code = "profile_blocked" if next_profile_id else "human_action_required"
        return Result(
            session_id=session_id,
            error=code,
            sessions=sessions,
            profile_id=profile_id,
            next_profile_id=next_profile_id,
        )
    except ProfilePoolExhaustedError:
        return Result(error="human_action_required")
    except KeyError:
        return Result(session_id=session_id, error="session_not_found")
    except Exception as error:
        log.exception("processing error")
        return Result(session_id=session_id, error=f"processing_error: {error}")


def main():
    browser = Browser(worker_id=WORKER_ID)
    connection = pika.BlockingConnection(pika.URLParameters(RABBITMQ_URL))
    channel = connection.channel()
    channel.exchange_declare(exchange=TASK_EXCHANGE, exchange_type="direct", durable=True)
    channel.queue_declare(queue=QUEUE, durable=True)
    channel.queue_bind(exchange=TASK_EXCHANGE, queue=QUEUE, routing_key=WORKER_ID)
    channel.basic_qos(prefetch_count=1)
    channel.confirm_delivery()

    def on_message(ch, method, properties, body):
        # pika BlockingConnection + prefetch_count=1 + синхронный on_message =>
        # ровно одна задача в любой момент; BEGIN/END в логе это фиксируют.
        t0 = time.monotonic()
        log.info("BEGIN tag=%s", method.delivery_tag)
        try:
            task = Task.model_validate_json(body)
            result = handle(browser, task)
        except Exception as error:
            result = Result(error=f"invalid_task: {error}")
        log.info("END tag=%s %.1fs err=%s", method.delivery_tag,
                 time.monotonic() - t0, result.error)
        if properties.reply_to and properties.correlation_id:
            ch.basic_publish(
                exchange="",
                routing_key=properties.reply_to,
                body=result.model_dump_json().encode(),
                properties=pika.BasicProperties(
                    correlation_id=properties.correlation_id,
                    content_type="application/json",
                    delivery_mode=2,
                ),
            )
        ch.basic_ack(method.delivery_tag)

    try:
        channel.basic_consume(queue=QUEUE, on_message_callback=on_message)
        channel.start_consuming()
    finally:
        browser.shutdown()
        connection.close()


if __name__ == "__main__":
    main()
