"""Validation of Razorpay API responses (never trust Telegram callback data)."""
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP


def expected_paise(amount):
    try:
        value = Decimal(str(amount))
        if not value.is_finite() or value <= 0:
            return None
        return int((value * 100).quantize(Decimal('1'), rounding=ROUND_HALF_UP))
    except (InvalidOperation, TypeError, ValueError):
        return None


def captured_payment(payment, payment_id, paise):
    return bool(
        isinstance(payment, dict)
        and isinstance(payment_id, str)
        and payment_id.startswith('pay_')
        and payment.get('id') == payment_id
        and payment.get('status') == 'captured'
        and payment.get('captured') is True
        and payment.get('currency') == 'INR'
        and type(payment.get('amount')) is int
        and payment['amount'] == paise
    )


def qr_payment_ids(qr_payload, expected):
    """Return captured payment ids reported by QR's payment-list endpoint."""
    if not isinstance(qr_payload, dict) or not isinstance(qr_payload.get('items'), list):
        return []
    return [p['id'] for p in qr_payload['items']
            if isinstance(p, dict) and p.get('status') == 'captured'
            and p.get('currency') == 'INR'
            and type(p.get('amount')) is int and p['amount'] == expected
            and isinstance(p.get('id'), str) and p['id'].startswith('pay_')]


def link_payment_ids(link, link_id, dep_id, expected):
    """Validate that the paid standard Payment Link is OUR exact deposit."""
    if not isinstance(link, dict) or not link_id or link.get('id') != link_id:
        return []
    if (link.get('status') != 'paid' or link.get('currency') != 'INR'
            or type(link.get('amount')) is not int or link['amount'] != expected
            or type(link.get('amount_paid')) is not int or link['amount_paid'] != expected
            or link.get('reference_id') != f'dep_{dep_id}'
            or link.get('accept_partial') is not False):
        return []
    payments = link.get('payments')
    if not isinstance(payments, list):
        return []
    valid = []
    for item in payments:
        if (isinstance(item, dict) and item.get('status') == 'captured'
                and item.get('plink_id') == link_id
                and type(item.get('amount')) is int and item['amount'] == expected
                and isinstance(item.get('payment_id'), str)
                and item['payment_id'].startswith('pay_')):
            valid.append(item['payment_id'])
    return valid
