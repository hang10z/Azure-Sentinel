import os
import json
from aiobotocore.session import get_session
import time
import logging
import azure.functions as func
import urllib.parse
from ..azure_storage_queue import AzureStorageQueueHelper
import traceback
import base64
from .. import constants
from helper import skip_processing_file, check_if_script_runs_too_long

AWS_KEY = constants.AWS_KEY
AWS_SECRET = constants.AWS_SECRET
AWS_REGION_NAME = constants.AWS_REGION_NAME
SQS_QUEUE_URL = constants.SQS_QUEUE_URL
VISIBILITY_TIMEOUT = 1800
LINE_SEPARATOR = constants.LINE_SEPARATOR
MAX_SCRIPT_EXEC_TIME_MINUTES = constants.MAX_SCRIPT_EXEC_TIME_MINUTES
FLOW_LOGS_CUSTOM_TABLE = constants.FLOW_LOGS_CUSTOM_TABLE
AUDIT_LOGS_CUSTOM_TABLE = constants.AUDIT_LOGS_CUSTOM_TABLE
AZURE_STORAGE_CONNECTION_STRING = constants.AZURE_STORAGE_CONNECTION_STRING
MAX_QUEUE_MESSAGES_MAIN_QUEUE = constants.MAX_QUEUE_MESSAGES_MAIN_QUEUE
ALLOWED_TRAFFIC = constants.ALLOWED_TRAFFIC
POTENTIALLY_BLOCKED_TRAFFIC = constants.POTENTIALLY_BLOCKED_TRAFFIC
BLOCKED_TRAFFIC = constants.BLOCKED_TRAFFIC
ALL_TRAFFIC = constants.ALL_TRAFFIC
MAX_ACCUMULATED_FILE_SIZE = 500 * 1000  # 500kb
MAX_AZURE_QUEUE_SIZE_PER_ELEMENT_LIMIT = 64 * 1000  # 64KB
AZURE_QUEUE_SIZE_PER_ELEMENT_LIMIT = (
    0.5 * MAX_AZURE_QUEUE_SIZE_PER_ELEMENT_LIMIT
)  # 32kb
SQS_FILES_READ_LIMIT = constants.SQS_FILES_READ_LIMIT
LOGS_TO_CONSUME = constants.LOGS_TO_CONSUME.downcase
NETWORK_TRAFFIC_LOGS_TO_CONSUME = (
    constants.NETWORK_TRAFFIC_TO_CONSUME
)  # this has to be an array of choices
FLOW_EVENTS = constants.FLOW_EVENTS
AUDIT_EVENTS = constants.AUDIT_EVENTS

sentinel_connectors = {}


# Defining the SQS Client object based on AWS Credentials
def _create_sqs_client():
    sqs_session = get_session()
    return sqs_session.create_client(
        "sqs",
        region_name=AWS_REGION_NAME,
        aws_access_key_id=AWS_KEY,
        aws_secret_access_key=AWS_SECRET,
    )


# SQS record info can sometimes be encompassed within a SNS notification in certain deployments, hence
# its better to make this method compatible with both SNS topics and SQS
def process_sqs_body(body):

    # Check if the message is from SNS
    try:
        record = None
        if body.get("Type"):
            # Extract the actual SQS message from the SNS message
            record = json.loads(body["Message"])
            record = record["Records"][0]
        else:
            # Assume the message is directly from SQS
            record = body["Records"][0]
        file_path = record["s3"]["object"]["key"]  # full path to s3
        file_size = record["s3"]["object"]["size"]  # in bytes
        bucket_name = record["s3"]["bucket"]["name"]
    except Exception as e:
        logging.error("Error {} observed when parsing queue body".format(e))
        return None, None, None

    return file_path, file_size, bucket_name


def get_string_size(file_arr):
    message_bytes = str(file_arr).encode("ascii")
    base64_bytes = base64.b64encode(message_bytes)
    return len(base64_bytes)


def split_request_payload(payload):
    mid = len(payload) // 2
    return payload[:mid], payload[mid:]


def enqueue_message_helper(mainQueueHelper, backlogQueueHelper, file_arr):
    if mainQueueHelper.get_queue_current_count() >= MAX_QUEUE_MESSAGES_MAIN_QUEUE:
        backlogQueueHelper.send_to_queue(file_arr, True)
    else:
        mainQueueHelper.send_to_queue(file_arr, True)


def skip_processing_network_traffic_file(file_path):
    """
    Network traffic is stored in s3 in the following format
    pce/pd=0/..
    pce/pd=1/..
    pce/pd=2/..
    pce/pd=3/..

    This method will be used to filter which network traffic logs should be consumed/ingested
    Args:
        file_path: file path should contain the mapping info

    """
    pd_mapping = {
        ALLOWED_TRAFFIC: "pd=0",
        POTENTIALLY_BLOCKED_TRAFFIC: "pd=1",
        BLOCKED_TRAFFIC: "pd=2",
    }
    if ALL_TRAFFIC in NETWORK_TRAFFIC_LOGS_TO_CONSUME:
        return False

    # Suppose file_pd is "pd=2", which means customer wants to ingest only blocked traffic
    # and file_path contains pd=0 (allowed), then this method returns
    for pd in NETWORK_TRAFFIC_LOGS_TO_CONSUME:
        if pd_mapping[pd] in file_path:
            return False

    # skip the file if it doesnt match the conditions
    return True


# Start unloading contents of file_arr onto azure queues
# There is a main queue and a backlog queue to choose from
# Can place upto 64kb per queue element, so in case, size is larger, split it into smaller chunks
#
async def enqueue_message_azure(mainQueueHelper, backlogQueueHelper, file_arr):
    if len(file_arr) > 0:
        if (
            get_string_size(file_arr) >= AZURE_QUEUE_SIZE_PER_ELEMENT_LIMIT
        ):  # greater than 32kb; each queue element can be upto 64kb in size
            first_half, second_half = split_request_payload(file_arr)
            enqueue_message_helper(mainQueueHelper, backlogQueueHelper, first_half)
            enqueue_message_helper(mainQueueHelper, backlogQueueHelper, second_half)
        else:
            enqueue_message_helper(mainQueueHelper, backlogQueueHelper, file_arr)


async def main(mytimer: func.TimerRequest):
    """
        Ensure flushing messages happens during these times:
        1. When script has reached 90% of execution time
        2. When files accumulated has crossed MAX_ACCUMULATED_FILE_SIZE
        3. When there are no more messages in SQS
        4. Check if user chose a specific log type or wants all logs types to be processed, in this case, lesser storage

    Args:
        mytimer (func.TimerRequest): timer set in function app
    """

    script_start_time = int(time.time())

    async with _create_sqs_client() as client:
        mainQueueHelper = AzureStorageQueueHelper(
            connectionString=AZURE_STORAGE_CONNECTION_STRING,
            queueName="python-queue-items",
        )
        backlogQueueHelper = AzureStorageQueueHelper(
            connectionString=AZURE_STORAGE_CONNECTION_STRING,
            queueName="python-queue-items-backlog",
        )
        files_processed = 0
        accumulated_file_size = (
            0  # logic is to accumulate file sizes upto MAX_ACCUMULATED_FILE_SIZE
        )
        file_arr = []

        while True:
            try:
                # This should return MaxNumberOfMessages message from SQS only
                response = await client.receive_message(
                    QueueUrl=SQS_QUEUE_URL,
                    MaxNumberOfMessages=10,
                    WaitTimeSeconds=2,
                    VisibilityTimeout=VISIBILITY_TIMEOUT,
                )

                if "Messages" in response:  # this is an array
                    for msg in response["Messages"]:
                        body_obj = json.loads(msg["Body"])
                        file_path, file_size, bucket_name = process_sqs_body(body_obj)

                        if (
                            file_path is None
                        ):  # case when sqs message doesnt have any records in it
                            return

                        # decide whether audit or network traffic or both should be consumed or not
                        if skip_processing_file(file_path):
                            logging.warn(
                                "[AWSQueue] Skipping file since logs to be consumed is {}, but file is {}".format(
                                    LOGS_TO_CONSUME, file_path
                                )
                            )
                            continue

                        # decide which network traffic file paths to consume
                        if skip_processing_network_traffic_file(file_path):
                            logging.warn(
                                "[AWSQueue] Skipping network traffic file since logs to be consumed is {}, but file is {}".format(
                                    NETWORK_TRAFFIC_LOGS_TO_CONSUME, file_path
                                )
                            )
                            continue

                        files_processed += 1

                        accumulated_file_size += file_size

                        file_arr.append(
                            {
                                "link": urllib.parse.unquote(file_path),
                                "file_size": file_size,
                                "bucket_name": bucket_name,
                                "sqs_message_id": msg["MessageId"],
                            }
                        )

                        try:
                            await client.delete_message(
                                QueueUrl=SQS_QUEUE_URL,
                                ReceiptHandle=msg["ReceiptHandle"],
                            )
                        except Exception as e:
                            logging.error(
                                "[AWSQueue] Error during deleting message with MessageId {} from queue. Bucket: {}. Error: {}".format(
                                    msg["MessageId"], body_obj["s3"]["bucket"], e
                                )
                            )
                            continue

                        # ensure to return if files processed are more than the limit
                        if files_processed >= SQS_FILES_READ_LIMIT:
                            logging.warn(
                                "[AWSQueue] Have processed {} files and hence exiting".format(
                                    files_processed
                                )
                            )
                            await enqueue_message_azure(
                                mainQueueHelper, backlogQueueHelper, file_arr
                            )
                            file_stats = {
                                "Trigger": "Timer",
                                "Type": "FileStats",
                                "file_count": len(file_arr),
                                "azure_queue_size": get_string_size(file_arr),
                                "aggregated_file_size": accumulated_file_size,
                            }
                            logging.info(json.dumps(file_stats))
                            return

                        if check_if_script_runs_too_long(0.90, script_start_time):
                            logging.warn(
                                "[AWSQueue]SQS Queue manager has run close to 90 percentage of max time. Flushing files to queue before termination"
                            )
                            await enqueue_message_azure(
                                mainQueueHelper, backlogQueueHelper, file_arr
                            )
                            file_stats = {
                                "Trigger": "Timer",
                                "Type": "FileStats",
                                "file_count": len(file_arr),
                                "azure_queue_size": get_string_size(file_arr),
                                "aggregated_file_size": accumulated_file_size,
                            }
                            logging.info(json.dumps(file_stats))
                            return

                        # decide whether file size accumulated so far has reached limit or not
                        # if not, wait, else if nothing else left, just add it to queue and terminate function

                        if accumulated_file_size >= MAX_ACCUMULATED_FILE_SIZE:
                            await enqueue_message_azure(
                                mainQueueHelper, backlogQueueHelper, file_arr
                            )
                            logging.info(
                                "[AWSQueue] Crossed the max file size limit, enqueing messages in azure queue"
                            )
                            file_stats = {
                                "Trigger": "Timer",
                                "Type": "FileStats",
                                "file_count": len(file_arr),
                                "azure_queue_size": get_string_size(file_arr),
                                "aggregated_file_size": accumulated_file_size,
                            }
                            logging.info(json.dumps(file_stats))
                            accumulated_file_size = 0
                            file_arr.clear()

                else:
                    logging.info(
                        "[AWSQueue] There are no messages in SQS, attempting to enqueue files seen so far"
                    )
                    await enqueue_message_azure(
                        mainQueueHelper, backlogQueueHelper, file_arr
                    )
                    file_stats = {
                        "Trigger": "Timer",
                        "Type": "FileStats",
                        "file_count": len(file_arr),
                        "azure_queue_size": get_string_size(file_arr),
                        "aggregated_file_size": accumulated_file_size,
                    }
                    logging.info(json.dumps(file_stats))
                    return

            except Exception as e:
                logging.warning(traceback.format_exc())
                return
