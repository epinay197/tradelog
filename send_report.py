"""
Simple email sender for Claude scheduled tasks.
Uses Gmail SMTP with app password from environment or config.

Usage:
  python send_report.py --to kottowc@gmail.com --subject "V5 Report" --body "report text"
  python send_report.py --to kottowc@gmail.com --subject "V5 Report" --body-file report.html --html
"""
import argparse, smtplib, os, json, sys
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart
from pathlib import Path

def get_creds():
    """Get SMTP credentials from environment or config file."""
    smtp_user = os.environ.get('SMTP_USER', '')
    smtp_pass = os.environ.get('SMTP_APP_PASSWORD', '')
    if smtp_user and smtp_pass:
        return smtp_user, smtp_pass

    # Try config.json in tradelog dir
    cfg_path = Path(__file__).parent / 'smtp_config.json'
    if cfg_path.exists():
        with open(cfg_path) as f:
            cfg = json.load(f)
        return cfg.get('smtp_user', ''), cfg.get('smtp_app_password', '')

    return '', ''

def send(to, subject, body, html=False):
    smtp_user, smtp_pass = get_creds()
    if not smtp_user or not smtp_pass:
        print("ERROR: No SMTP credentials. Set SMTP_USER/SMTP_APP_PASSWORD env vars or create smtp_config.json")
        sys.exit(1)

    msg = MIMEMultipart('alternative')
    msg['Subject'] = subject
    msg['From'] = smtp_user
    msg['To'] = to

    content_type = 'html' if html else 'plain'
    msg.attach(MIMEText(body, content_type, 'utf-8'))

    try:
        with smtplib.SMTP_SSL('smtp.gmail.com', 465, timeout=15) as s:
            s.login(smtp_user, smtp_pass)
            s.sendmail(smtp_user, to, msg.as_string())
        print(f"Email sent to {to}: {subject}")
    except Exception as e:
        print(f"Email failed: {e}")
        sys.exit(1)

if __name__ == '__main__':
    p = argparse.ArgumentParser()
    p.add_argument('--to', required=True)
    p.add_argument('--subject', required=True)
    p.add_argument('--body', default='')
    p.add_argument('--body-file', default='')
    p.add_argument('--html', action='store_true')
    args = p.parse_args()

    body = args.body
    if args.body_file:
        with open(args.body_file) as f:
            body = f.read()

    send(args.to, args.subject, body, args.html)
