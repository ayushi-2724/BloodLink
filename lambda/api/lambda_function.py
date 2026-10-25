import json
import os
import uuid
import logging
from datetime import datetime, timezone, timedelta
from decimal import Decimal

import boto3

logger = logging.getLogger()
logger.setLevel(logging.INFO)

TABLE_NAME = os.environ.get("TABLE_NAME", "BloodRequests")
DONOR_TOPIC_ARN = os.environ.get("DONOR_TOPIC_ARN", "")

dynamodb = boto3.resource("dynamodb")
table = dynamodb.Table(TABLE_NAME)
sns = boto3.client("sns")

IST = timezone(timedelta(hours=5, minutes=30))
BLOOD_GROUPS = ["A+", "A-", "B+", "B-", "AB+", "AB-", "O+", "O-"]


def now_ist():
    return datetime.now(IST).isoformat(timespec="seconds")


def to_native(obj):
    if isinstance(obj, list):
        return [to_native(i) for i in obj]
    if isinstance(obj, dict):
        return {k: to_native(v) for k, v in obj.items()}
    if isinstance(obj, Decimal):
        return int(obj) if obj % 1 == 0 else float(obj)
    return obj


def respond(status, body):
    return {
        "statusCode": status,
        "headers": {"Content-Type": "application/json"},
        "body": json.dumps(to_native(body)),
    }


def get_claims(event):
    try:
        return event["requestContext"]["authorizer"]["jwt"]["claims"]
    except (KeyError, TypeError):
        return {}


def parse_body(event):
    raw = event.get("body") or "{}"
    if event.get("isBase64Encoded"):
        import base64
        raw = base64.b64decode(raw).decode("utf-8")
    try:
        data = json.loads(raw)
    except json.JSONDecodeError:
        raise ValueError("Request body must be valid JSON")
    if not isinstance(data, dict):
        raise ValueError("Request body must be a JSON object")
    return data


def validate_request(data):
    errors = []
    patient = str(data.get("patientName", "")).strip()
    group = str(data.get("bloodGroup", "")).strip().upper()
    hospital = str(data.get("hospital", "")).strip()
    city = str(data.get("city", "")).strip()
    phone = str(data.get("contactPhone", "")).strip()
    required_by = str(data.get("requiredBy", "")).strip()
    notes = str(data.get("notes", "") or "").strip()

    if not patient or len(patient) > 100:
        errors.append("Patient name is required (max 100 characters)")
    if group not in BLOOD_GROUPS:
        errors.append("Blood group must be one of " + ", ".join(BLOOD_GROUPS))
    if not hospital or len(hospital) > 150:
        errors.append("Hospital is required (max 150 characters)")
    if not city or len(city) > 80:
        errors.append("City is required (max 80 characters)")
    try:
        units = int(data.get("unitsNeeded"))
        if units < 1 or units > 10:
            raise ValueError()
    except (TypeError, ValueError):
        units = None
        errors.append("Units needed must be a whole number from 1 to 10")
    if not (phone.isdigit() and len(phone) == 10):
        errors.append("Contact phone must be exactly 10 digits")
    try:
        datetime.strptime(required_by, "%Y-%m-%d")
    except ValueError:
        errors.append("Required-by date must be in YYYY-MM-DD format")
    if len(notes) > 500:
        errors.append("Notes must be at most 500 characters")

    if errors:
        raise ValueError("; ".join(errors))
    return {
        "patientName": patient,
        "bloodGroup": group,
        "hospital": hospital,
        "city": city,
        "unitsNeeded": units,
        "contactPhone": phone,
        "requiredBy": required_by,
        "notes": notes,
    }


# ---------------------------------------------------------------------------
# SNS helpers
# One email subscription per user on the BloodLink topic. A subscription
# "filter policy" decides what that person receives:
#   donor           -> {"recipient": ["all-donors", "<email>"]}  (alerts + personal mail)
#   not a donor     -> {"recipient": ["<email>"]}                (personal mail only)
# Broadcasts carry recipient=all-donors, personal mails carry recipient=<email>.
# ---------------------------------------------------------------------------
ALL_DONORS = "all-donors"


def donor_filter(email):
    return json.dumps({"recipient": [ALL_DONORS, email]})


def personal_filter(email):
    return json.dumps({"recipient": [email]})


CANCELLED = "cancelled-by-user"


def cancelled_filter():
    return json.dumps({"recipient": [CANCELLED]})


def publish(subject, message, recipient=ALL_DONORS):
    if not DONOR_TOPIC_ARN:
        logger.warning("DONOR_TOPIC_ARN not set; skipping publish")
        return None
    resp = sns.publish(
        TopicArn=DONOR_TOPIC_ARN, Subject=subject[:100], Message=message,
        MessageAttributes={"recipient": {"DataType": "String", "StringValue": recipient}},
    )
    logger.info("SNS publish OK (to %s): MessageId=%s", recipient, resp.get("MessageId"))
    return resp.get("MessageId")


def contact_key(email):
    return "CONTACT#" + email


def find_subscription(email):
    """Returns (subscription_arn or None, confirmed bool)."""
    target = email.strip().lower()
    kwargs = {"TopicArn": DONOR_TOPIC_ARN}
    while True:
        page = sns.list_subscriptions_by_topic(**kwargs)
        for s in page.get("Subscriptions", []):
            if s.get("Protocol") == "email" and s.get("Endpoint", "").strip().lower() == target:
                arn = s.get("SubscriptionArn", "")
                if arn.startswith("arn:aws:sns:"):
                    return arn, True
                stored = table.get_item(Key={"requestId": contact_key(target)}).get("Item") or {}
                return stored.get("subscriptionArn"), False
        if not page.get("NextToken"):
            return None, False
        kwargs["NextToken"] = page["NextToken"]


def get_filter(arn):
    attrs = sns.get_subscription_attributes(SubscriptionArn=arn).get("Attributes", {})
    raw = attrs.get("FilterPolicy")
    return json.loads(raw).get("recipient", []) if raw else None


def create_subscription(email, filter_json):
    try:
        resp = sns.subscribe(
            TopicArn=DONOR_TOPIC_ARN, Protocol="email", Endpoint=email,
            Attributes={"FilterPolicy": filter_json}, ReturnSubscriptionArn=True,
        )
        arn = resp.get("SubscriptionArn")
    except Exception as e:
        # an older unconfirmed subscription without a filter exists -> reuse it
        logger.info("Subscribe with filter failed (%s); reusing existing subscription", e)
        arn = sns.subscribe(TopicArn=DONOR_TOPIC_ARN, Protocol="email", Endpoint=email,
                            ReturnSubscriptionArn=True).get("SubscriptionArn")
        sns.set_subscription_attributes(SubscriptionArn=arn, AttributeName="FilterPolicy",
                                        AttributeValue=filter_json)
    table.put_item(Item={"requestId": contact_key(email.strip().lower()), "email": email,
                         "subscriptionArn": arn, "createdAt": now_ist()})
    logger.info("Subscribed %s: %s", email, arn)
    return arn


def donor_state(email):
    """none | pending | donor | not_donor"""
    arn, confirmed = find_subscription(email)
    if not arn and not confirmed:
        return "none", None
    if not confirmed:
        stored = table.get_item(Key={"requestId": contact_key(email.strip().lower())}).get("Item") or {}
        return ("none" if stored.get("cancelled") else "pending"), arn
    recipients = get_filter(arn)
    if recipients is not None and email not in recipients and ALL_DONORS not in recipients:
        # confirmed after cancelling -> keep only the personal channel
        sns.set_subscription_attributes(SubscriptionArn=arn, AttributeName="FilterPolicy",
                                        AttributeValue=personal_filter(email))
        return "not_donor", arn
    if recipients is None:  # old subscription without a filter -> it is a donor; add filter
        sns.set_subscription_attributes(SubscriptionArn=arn, AttributeName="FilterPolicy",
                                        AttributeValue=donor_filter(email))
        return "donor", arn
    return ("donor" if ALL_DONORS in recipients else "not_donor"), arn


# ---------------------------------------------------------------------------
# Requests
# ---------------------------------------------------------------------------
def list_requests(event):
    items = []
    kwargs = {}
    while True:
        resp = table.scan(**kwargs)
        items.extend(resp.get("Items", []))
        if "LastEvaluatedKey" not in resp:
            break
        kwargs["ExclusiveStartKey"] = resp["LastEvaluatedKey"]
    me = get_claims(event).get("sub")
    items = [i for i in items if not str(i.get("requestId", "")).startswith("CONTACT#")]
    for i in items:
        responders = i.get("responders", [])
        i["responderCount"] = len(responders)
        i["iOffered"] = any(r.get("sub") == me for r in responders)
        if i.get("createdBy") == me:  # only the owner sees who offered
            i["responders"] = [{"email": r.get("email"), "at": r.get("at")} for r in responders]
        else:
            i.pop("responders", None)
    items.sort(key=lambda x: x.get("createdAt", ""), reverse=True)
    return respond(200, {"items": items, "count": len(items)})


def create_request(event):
    claims = get_claims(event)
    sub = claims.get("sub")
    email = claims.get("email", "")
    if not sub:
        return respond(401, {"message": "Unauthorized"})
    try:
        data = validate_request(parse_body(event))
    except ValueError as e:
        return respond(400, {"message": str(e)})

    item = dict(data)
    item.update({
        "requestId": str(uuid.uuid4()),
        "status": "Open",
        "createdBy": sub,
        "createdByEmail": email,
        "createdAt": now_ist(),
        "responders": [],
    })
    table.put_item(Item=item)
    logger.info("Created request %s", item["requestId"])

    subject = "URGENT: {} blood needed at {}".format(item["bloodGroup"], item["hospital"])
    lines = [
        "URGENT BLOOD REQUEST - BloodLink",
        "",
        "Patient: " + item["patientName"],
        "Blood group: " + item["bloodGroup"],
        "Units needed: " + str(item["unitsNeeded"]),
        "Hospital: " + item["hospital"],
        "City: " + item["city"],
        "Required by: " + item["requiredBy"],
        "Contact phone: " + item["contactPhone"],
    ]
    if item["notes"]:
        lines.append("Notes: " + item["notes"])
    lines += ["", "Call {} if you can donate, or open BloodLink and press \"I can donate\".".format(item["contactPhone"]),
              "", "Request ID: " + item["requestId"]]
    try:
        publish(subject, "\n".join(lines))
    except Exception:
        logger.exception("SNS publish failed for %s", item["requestId"])

    # Make sure the requester can receive personal "donor found" emails
    note = ""
    if email:
        try:
            arn, confirmed = find_subscription(email)
            if not arn and not confirmed:
                create_subscription(email, personal_filter(email))
                note = " We also sent a one-time email to {} - confirm it to get an email when a donor offers to help.".format(email)
            elif not confirmed:
                note = " Confirm the AWS email in your inbox to get an email when a donor offers to help."
        except Exception:
            logger.exception("Could not set up requester notifications for %s", email)
    return respond(201, {"message": "Request posted and donors alerted." + note, "item": item})


def respond_to_request(event):
    claims = get_claims(event)
    sub = claims.get("sub")
    email = claims.get("email", "")
    if not sub:
        return respond(401, {"message": "Unauthorized"})
    request_id = (event.get("pathParameters") or {}).get("id", "")
    item = table.get_item(Key={"requestId": request_id}).get("Item") if request_id else None
    if not item or request_id.startswith("CONTACT#"):
        return respond(404, {"message": "Request not found"})
    if item.get("createdBy") == sub:
        return respond(400, {"message": "This is your own request."})
    if item.get("status") != "Open":
        return respond(409, {"message": "This request is already fulfilled. Thank you anyway!"})
    if any(r.get("sub") == sub for r in item.get("responders", [])):
        return respond(409, {"message": "You have already offered to donate for this request. The requester has been informed."})

    entry = {"sub": sub, "email": email, "at": now_ist()}
    table.update_item(
        Key={"requestId": request_id},
        UpdateExpression="SET responders = list_append(if_not_exists(responders, :empty), :new)",
        ConditionExpression="#s = :o",
        ExpressionAttributeNames={"#s": "status"},
        ExpressionAttributeValues={":new": [entry], ":empty": [], ":o": "Open"},
    )
    logger.info("Donor %s offered for request %s", email, request_id)

    owner = item.get("createdByEmail")
    if owner:
        subject = "Donor found for {} ({})".format(item["patientName"], item["bloodGroup"])
        message = (
            "Good news - a donor is available!\n\n"
            "A BloodLink donor has offered to donate for your request:\n"
            "Patient: {}\nBlood group: {}\nHospital: {}, {}\n\n"
            "Donor email: {}\n\n"
            "Please contact the donor as soon as possible. They may also call you on {}.\n"
            "When the need is met, open BloodLink and press \"Mark as Fulfilled\".\n\nRequest ID: {}"
        ).format(item["patientName"], item["bloodGroup"], item["hospital"], item["city"],
                 email, item["contactPhone"], request_id)
        try:
            publish(subject, message, recipient=owner)
        except Exception:
            logger.exception("Donor-found email failed for %s", request_id)
    return respond(200, {"message": "Thank you! The requester has been notified. Please also call {} to coordinate.".format(item["contactPhone"])})


def fulfill_request(event):
    claims = get_claims(event)
    sub = claims.get("sub")
    if not sub:
        return respond(401, {"message": "Unauthorized"})
    request_id = (event.get("pathParameters") or {}).get("id", "")
    if not request_id:
        return respond(400, {"message": "Request id is required"})

    resp = table.get_item(Key={"requestId": request_id})
    item = resp.get("Item")
    if not item or request_id.startswith("CONTACT#"):
        return respond(404, {"message": "Request not found"})
    if item.get("createdBy") != sub:
        return respond(403, {"message": "Only the person who posted this request can mark it fulfilled"})
    if item.get("status") == "Fulfilled":
        return respond(409, {"message": "This request is already fulfilled"})

    fulfilled_at = now_ist()
    updated = table.update_item(
        Key={"requestId": request_id},
        UpdateExpression="SET #s = :f, fulfilledAt = :t",
        ConditionExpression="#s = :o",
        ExpressionAttributeNames={"#s": "status"},
        ExpressionAttributeValues={":f": "Fulfilled", ":t": fulfilled_at, ":o": "Open"},
        ReturnValues="ALL_NEW",
    )["Attributes"]
    logger.info("Fulfilled request %s", request_id)

    subject = "Request fulfilled - thank you ({} at {})".format(item["bloodGroup"], item["hospital"])
    message = (
        "Request fulfilled - thank you!\n\n"
        "The {} blood request for {} at {}, {} has been fulfilled.\n"
        "No more donors are needed for this request.\n\n"
        "Thank you for being part of BloodLink.\n\nRequest ID: {}"
    ).format(item["bloodGroup"], item["patientName"], item["hospital"], item["city"], request_id)
    try:
        publish(subject, message)
    except Exception:
        logger.exception("SNS publish failed for fulfil %s", request_id)
    return respond(200, {"message": "Request marked as fulfilled", "item": updated})


# ---------------------------------------------------------------------------
# Donor status / become donor / opt out / cancel
# ---------------------------------------------------------------------------
def donor_status(event):
    email = get_claims(event).get("email")
    if not email:
        return respond(400, {"message": "No email found in your login token"})
    state, _ = donor_state(email)
    return respond(200, {"status": state, "email": email})


def subscribe(event):
    email = get_claims(event).get("email")
    if not email:
        return respond(400, {"message": "No email found in your login token"})
    state, arn = donor_state(email)

    if state == "donor":
        return respond(200, {"status": "donor",
                             "message": "You are already a donor. You will receive all emergency alerts at {}.".format(email)})
    if state == "pending":
        return respond(200, {"status": "pending",
                             "message": "A confirmation email was already sent to {}. Please open it (check Spam too) and click 'Confirm subscription'.".format(email)})
    if state == "not_donor":  # confirmed email channel exists -> just switch alerts on, no new confirmation
        sns.set_subscription_attributes(SubscriptionArn=arn, AttributeName="FilterPolicy",
                                        AttributeValue=donor_filter(email))
        try:
            publish("Welcome to BloodLink donors",
                    "Thank you for becoming a BloodLink donor!\n\nYou will now receive emergency blood request alerts at {}.\n"
                    "You can opt out any time from the BloodLink website.".format(email), recipient=email)
        except Exception:
            logger.exception("Welcome email failed for %s", email)
        return respond(200, {"status": "donor", "message": "You are now a donor! You will receive emergency alerts at {}.".format(email)})

    if arn:  # a cancelled pending request exists -> re-activate it and resend the confirmation email
        sns.set_subscription_attributes(SubscriptionArn=arn, AttributeName="FilterPolicy",
                                        AttributeValue=donor_filter(email))
        sns.subscribe(TopicArn=DONOR_TOPIC_ARN, Protocol="email", Endpoint=email,
                      Attributes={"FilterPolicy": donor_filter(email)}, ReturnSubscriptionArn=True)
        table.update_item(Key={"requestId": contact_key(email.strip().lower())},
                          UpdateExpression="REMOVE cancelled")
    else:
        create_subscription(email, donor_filter(email))
    return respond(200, {"status": "pending",
                         "message": "Check your inbox ({}) and click 'Confirm subscription' to start receiving donor alerts.".format(email)})


def optout(event):
    email = get_claims(event).get("email")
    if not email:
        return respond(400, {"message": "No email found in your login token"})
    state, arn = donor_state(email)

    if state == "donor":
        # keep the personal channel (for 'donor found' mails), stop donor alerts
        sns.set_subscription_attributes(SubscriptionArn=arn, AttributeName="FilterPolicy",
                                        AttributeValue=personal_filter(email))
        try:
            publish("You are no longer a BloodLink donor",
                    "Hello,\n\nAs requested, you have been removed from the BloodLink donor list.\n"
                    "You will no longer receive emergency blood request alerts at {}.\n\n"
                    "Thank you for the time you were a donor - it truly helps save lives.\n"
                    "You can become a donor again any time from the BloodLink website.\n\n- Team BloodLink".format(email),
                    recipient=email)
        except Exception:
            logger.exception("Opt-out email failed for %s", email)
        return respond(200, {"status": "not_donor",
                             "message": "You are no longer a donor. A confirmation email has been sent to {}.".format(email)})

    if state == "pending":
        if not arn:
            return respond(409, {"message": "Could not find your pending request. It will expire automatically in 3 days if not confirmed."})
        # SNS cannot delete an unconfirmed subscription, so make it receive nothing
        sns.set_subscription_attributes(SubscriptionArn=arn, AttributeName="FilterPolicy",
                                        AttributeValue=cancelled_filter())
        table.update_item(Key={"requestId": contact_key(email.strip().lower())},
                          UpdateExpression="SET cancelled = :t", ExpressionAttributeValues={":t": True})
        logger.info("Pending donor request cancelled for %s", email)
        return respond(200, {"status": "none",
                             "message": "Your request to become a donor has been cancelled by you. You will not receive donor alerts, even if you click the old confirmation link."})

    return respond(200, {"status": state, "message": "You are not a donor right now."})


def lambda_handler(event, context):
    route = event.get("routeKey", "")
    logger.info("Route: %s", route)
    try:
        if route == "GET /requests":
            return list_requests(event)
        if route == "POST /requests":
            return create_request(event)
        if route == "PUT /requests/{id}/fulfill":
            return fulfill_request(event)
        if route == "POST /requests/{id}/respond":
            return respond_to_request(event)
        if route == "POST /subscribe":
            return subscribe(event)
        if route == "GET /donor":
            return donor_status(event)
        if route == "POST /donor/optout":
            return optout(event)
        return respond(404, {"message": "Route not found: " + route})
    except Exception as e:
        code = getattr(e, "response", {}).get("Error", {}).get("Code", "")
        if code == "ConditionalCheckFailedException":
            return respond(409, {"message": "This request is already fulfilled"})
        logger.exception("Unhandled error")
        return respond(500, {"message": "Internal server error"})
