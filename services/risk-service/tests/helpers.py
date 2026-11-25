from pathlib import Path

SERVICE_ROOT = Path(__file__).parents[1]

NORMAL = {
    "amount_minor": 4000,
    "hour_of_day": 14,
    "account_age_days": 400,
    "transfers_last_hour": 0,
    "avg_amount_30d_minor": 4000,
    "is_new_recipient": False,
}
# An established account suddenly sending 15x its usual amount, at 3 a.m., to a new payee.
ACCOUNT_TAKEOVER = {
    "amount_minor": 60000,
    "hour_of_day": 3,
    "account_age_days": 500,
    "transfers_last_hour": 2,
    "avg_amount_30d_minor": 4000,
    "is_new_recipient": True,
}
