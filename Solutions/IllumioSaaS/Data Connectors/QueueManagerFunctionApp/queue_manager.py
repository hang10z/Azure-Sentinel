import os
import time
import logging
import azure.functions as func
from ..azure_storage_queue import AzureStorageQueueHelper
from .. import constants
from helper import check_if_script_runs_too_long

MAX_SCRIPT_EXEC_TIME_MINUTES = constants.MAX_SCRIPT_EXEC_TIME_MINUTES
AZURE_STORAGE_CONNECTION_STRING = constants.AZURE_STORAGE_CONNECTION_STRING
MAX_QUEUE_MESSAGES_MAIN_QUEUE = constants.MAX_QUEUE_MESSAGES_MAIN_QUEUE


async def main(mytimer: func.TimerRequest):
    script_start_time = int(time.time())
    mainQueueHelper = AzureStorageQueueHelper(
        connectionString=AZURE_STORAGE_CONNECTION_STRING, queueName="python-queue-items"
    )
    backlogQueueHelper = AzureStorageQueueHelper(
        connectionString=AZURE_STORAGE_CONNECTION_STRING,
        queueName="python-queue-items-backlog",
    )

    backlogQueueCount = backlogQueueHelper.get_queue_current_count()
    logging.info("File count in backlog queue is {}".format(backlogQueueCount))

    mainQueueCount = mainQueueHelper.get_queue_current_count()
    logging.info("File count in main queue is {}".format(mainQueueCount))

    while True:
        # attempt to exhaust backlog queue and feed enough to mainQueue
        if backlogQueueCount > 0:
            if mainQueueCount >= MAX_QUEUE_MESSAGES_MAIN_QUEUE:
                logging.info(
                    "Backlog queue and main queue are at limits, do not process any new messages from sqs"
                )
                return
            else:
                messageFromBacklog = backlogQueueHelper.deque_from_queue()
                if messageFromBacklog != None:
                    mainQueueHelper.send_to_queue(messageFromBacklog.content, False)
                    backlogQueueHelper.delete_queue_message(
                        messageFromBacklog.id, messageFromBacklog.pop_receipt
                    )

        else:
            return
        if check_if_script_runs_too_long(0.90, script_start_time):
            logging.warn(
                "Azure Queue manager has run close to 90 percentage of max time. Exiting"
            )
            return
