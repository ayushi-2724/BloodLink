import os
import logging
from datetime import datetime, timezone, timedelta

import boto3
from boto3.dynamodb.conditions import Attr

logger = logging.getLogger()
logger.setLevel(logging.INFO)

TABLE_NAME = os.environ.get("TABLE_NAME", "BloodRequests")
DONOR_TOPIC_ARN = os.environ.get("DONOR_TOPIC_ARN", "")

table = boto3.resource("dynamodb").Table(TABLE_NAME)
sns = boto3.client("sns")
IST = timezone(timedelta(hours=5, minutes=30))


def lambda_handler(event, context):
    items = []
    kwargs = {"FilterExpression": Attr("status").eq("Open")}
    while True:
        resp = table.scan(**kwargs)
        items.extend(resp.get("Items", []))
        if "LastEvaluatedKey" not in resp:
            break
        kwargs["ExclusiveStartKey"] = resp["LastEvaluatedKey"]

    if not items:
        logger.info("no open requests")
        return {"status": "ok", "open": 0}

    items.sort(key=lambda x: x.get("createdAt", ""), reverse=True)
    today = datetime.now(IST).strftime("%d %b %Y")
    lines = ["BloodLink daily digest - {}".format(today), "",
             "{} request(s) still need donors:".format(len(items)), ""]
    for i, it in enumerate(items, 1):
        lines.append("{}. {} - {} unit(s) for {} at {}, {}".format(
            i, it.get("bloodGroup"), it.get("unitsNeeded"), it.get("patientName"),
            it.get("hospital"), it.get("city")))
        lines.append("   Needed by {} | Call {}".format(it.get("requiredBy"), it.get("contactPhone")))
        lines.append("")
    lines.append("If you can donate, please call the number listed. Thank you!")

    resp = sns.publish(TopicArn=DONOR_TOPIC_ARN,
                       Subject="BloodLink daily digest: {} open request(s)".format(len(items))[:100],
                       Message="\n".join(lines),
                       MessageAttributes={"recipient": {"DataType": "String", "StringValue": "all-donors"}})
    logger.info("Digest published: %d open, MessageId=%s", len(items), resp.get("MessageId"))
    return {"status": "ok", "open": len(items)}
