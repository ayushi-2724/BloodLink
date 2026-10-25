# BloodLink – Emergency Blood Donor Alert System

A serverless web app built entirely on AWS free-tier services. People who need blood post a request, and every registered donor gets an email alert right away.

**Author:** Ayushi Sharma (24BYB0187)
**Live site:** https://bloodlink-web-814209304477.s3.ap-south-1.amazonaws.com/index.html

## Features
- Sign up, email verification and login with Amazon Cognito
- Post an emergency blood request; all donors get an instant email (Amazon SNS)
- Become a Donor / Opt out / Cancel a pending donor request
- "I can donate" button that emails the requester when a donor is found
- Request list with filters (status, blood group)
- Only the owner can mark a request as fulfilled (403 / 409 / 404 checks)
- Daily 9 AM IST digest of open requests (EventBridge Scheduler)
- CloudWatch dashboard and an error alarm emailed to the admin

## Architecture
Website (S3) → Cognito login (JWT) → API Gateway HTTP API (JWT authorizer) → Lambda `bloodlink-api` → DynamoDB `BloodRequests` + SNS emails.
EventBridge Scheduler → Lambda `bloodlink-daily-digest` → SNS. CloudWatch keeps logs and raises the error alarm.

## Folder structure
```
lambda/api/lambda_function.py           Main API (Python 3.12)
lambda/daily-digest/lambda_function.py  Daily digest function
website/                                index.html, style.css, app.js, config.js
iam-policies/                           IAM role and S3 bucket policies (JSON)
docs/BloodLink_AWS_Report.docx          Full project report with screenshots
```

## AWS services used
Amazon S3, Amazon Cognito, Amazon API Gateway, AWS Lambda, Amazon DynamoDB, Amazon SNS, Amazon EventBridge Scheduler, Amazon CloudWatch, AWS IAM. Region: ap-south-1 (Mumbai).

## API routes
| Method | Route | Purpose |
|---|---|---|
| GET | /requests | List requests |
| POST | /requests | Post a request and alert donors |
| PUT | /requests/{id}/fulfill | Mark own request fulfilled |
| POST | /requests/{id}/respond | "I can donate" |
| POST | /subscribe | Become a donor |
| GET | /donor | Donor status |
| POST | /donor/optout | Opt out / cancel |

The step-by-step setup is in `docs/BloodLink_AWS_Report.docx`.
