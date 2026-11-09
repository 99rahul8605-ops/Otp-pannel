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


def link_is_paid_for_deposit(link, link_id, dep_id, expected):
    """True when the Payment Link itself is fully paid and is OUR exact deposit."""
    return bool(
        isinstance(link, dict) and link_id and link.get('id') == link_id
        and link.get('status') == 'paid' and link.get('currency') == 'INR'
        and type(link.get('amount')) is int and link['amount'] == expected
        and type(link.get('amount_paid')) is int and link['amount_paid'] == expected
        and link.get('reference_id') == f'dep_{dep_id}'
        and link.get('accept_partial') is not True
    )


def link_payment_ids(link, link_id, dep_id, expected):
    """Captured payment ids listed inside a paid Payment Link (tolerant of
    Razorpay's field variations: payment_id/id, plink_id optional)."""
    if not link_is_paid_for_deposit(link, link_id, dep_id, expected):
        return []
    payments = link.get('payments')
    if not isinstance(payments, list):
        return []
    valid = []
    for item in payments:
        if not isinstance(item, dict) or item.get('status') != 'captured':
            continue
        if item.get('plink_id') not in (None, link_id):
            continue
        if type(item.get('amount')) is not int or item['amount'] != expected:
            continue
        pid = item.get('payment_id') or item.get('id')
        if isinstance(pid, str) and pid.startswith('pay_'):
            valid.append(pid)
    return valid
