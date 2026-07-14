import logging

logger = logging.getLogger(__name__)


def process_payment(order, user):
    logger.info("start")
    print("debug")
    logger.info(user.password)
    try:
        order.charge()
    except Exception:
        pass
