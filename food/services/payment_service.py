import logging
import uuid
import time
import requests
from django.conf import settings
from django.core.exceptions import ValidationError
from django.db import transaction
from food.models import Subscription

logger = logging.getLogger(__name__)

DEFAULT_REQUEST_TIMEOUT = 15


def _get_timeout():
    return getattr(settings, "PAYSTACK_TIMEOUT_SECONDS", DEFAULT_REQUEST_TIMEOUT)

def _build_headers():
    return {
        "Authorization": f"Bearer {settings.PAYSTACK_SECRET_KEY}",
        "Content-Type": "application/json",
    }

def _request_json(method, url, retries=2, **kwargs):
    for attempt in range(retries + 1):
        try:
            response = requests.request(method, url, timeout=_get_timeout(), **kwargs)
            return response, response.json()
        except requests.RequestException as exc:
            if attempt == retries:
                logger.exception("Paystack request failed after retries")
                raise ValidationError("Payment service unavailable. Try again.") from exc
            time.sleep(2 ** attempt)  # wait 1s, then 2s before retry


def initialize_order_payment(order):
    if order.payment_status == "PAID":
        raise ValidationError("Order has already been paid for")
    
    if order.payment_status == "PENDING" and order.payment_reference:
        raise ValidationError("Payment has already been initialized for this order")
    
    if order.status != "CONFIRMED":
        raise ValidationError("Only confirmed orders can be paid for")
    
    if order.total <= 0:
        raise ValidationError("Order total must be greater than 0")
    
    if not order.user.email:
        raise ValidationError("A valid email is required for payment")

    reference = f"ORDER-{order.id}-{uuid.uuid4().hex[:8].upper()}"

    payload = {
        "email": order.user.email,
        "amount": int(order.total * 100),
        "reference": reference,
        "callback_url": f"{settings.BASE_URL}/api/order/verify/{reference}/", 
        "metadata": {
            "order_id": order.id,
            "user_id": order.user.id,
            "username": order.user.username,
        }
    }

    response, data = _request_json(
        "POST",
        f"{settings.PAYSTACK_BASE_URL}/transaction/initialize",
        json=payload,
        headers=_build_headers(),
    )

    if not response.ok:
        logger.warning(
            "Paystack initialize failed for order_id=%s, status=%s", 
            order.id, 
            response.status_code
        )
        raise ValidationError("Payment initialization failed. Try again.")

    if not data.get("status"):
        raise ValidationError(data.get("message", "Payment initialization failed"))
    
    try:
        authorization_url = data["data"]["authorization_url"]
    except (TypeError, KeyError):
        raise ValidationError("Payment initialization failed. Try again.")

    with transaction.atomic():
        locked = type(order).objects.select_for_update().get(id=order.id)
        if locked.payment_status == "PAID":
            raise ValidationError("Order has already been paid for")
        locked.payment_reference = reference
        locked.payment_status = "PENDING"
        locked.save(update_fields=["payment_reference", "payment_status", "updated_at"])
    
    logger.info(
        f"Payment initialized for order {order.id} "
        f"by user {order.user.username} — ref: {reference}"
    )

    return authorization_url, reference

def verify_order_payment(reference):
    if not reference:
        raise ValidationError("Payment reference is required")

    response, data = _request_json(
        "GET",
        f"{settings.PAYSTACK_BASE_URL}/transaction/verify/{reference}",
        headers=_build_headers(),
    )

    if response.status_code != 200:
        logger.warning("Paystack verify failed: %s", data)
        raise ValidationError("Payment verification failed. Try again.")

    if not data.get("status"):
        raise ValidationError(data.get("message", "Verification failed."))
    
    try:
        payment_data = data["data"]
    except (TypeError, KeyError):
        raise ValidationError("Payment verification failed. Try again.")
    
    logger.info(
        f"Payment verification successful for reference {reference} "
        f"— status: {payment_data.get('status')} "
        f"amount: {payment_data.get('amount')} "   
    )

    return payment_data


def initialize_vendor_subscription_payment(vendor, plan):
    if plan.name == "FREE":
        raise ValidationError("No payment needed for free plan")

    if plan.price <= 0:
        raise ValidationError("Invalid plan price")
    
    try:
        subscription = vendor.subscription
        if subscription.is_valid() and subscription.plan == plan:
            raise ValidationError("You are already subscribed to this plan")
    except Subscription.DoesNotExist:
        pass

    reference = f"SUB-{vendor.id}-{uuid.uuid4().hex[:8].upper()}"

    payload = {
        "email": vendor.user.email,
        "amount": int(plan.price * 100), 
        "reference": reference,
        "callback_url": f"{settings.BASE_URL}/api/vendor/subscription/verify/{reference}/",
        "metadata": {
            "vendor_id": vendor.id,
            "plan_id": plan.id,
            "plan_name": plan.name,
            "business_name": vendor.business_name,
        }
    }

    response, data = _request_json(
        "POST",
        f"{settings.PAYSTACK_BASE_URL}/transaction/initialize",
        json=payload,
        headers=_build_headers()
    )
    
    if not response.ok:
        logger.warning(
            "Paystack initialize failed for plan_id=%s, status=%s", 
            plan.id, 
            response.status_code
        )
        raise ValidationError("Payment initialization failed. Try again.")
    
    if not data.get("status"):
        raise ValidationError(data.get("message", "Payment initialization failed"))
    
    try:
        authorization_url = data["data"]["authorization_url"]
    except (TypeError, KeyError):
        raise ValidationError("Payment initialization failed. Try again.")
    
    logger.info(
        f"Payment initialized for plan {plan.id} "
        f"by vendor {vendor.business_name} — ref: {reference}"
    )

    return authorization_url, reference


def verify_vendor_subscription_payment(reference):
    if not reference:
        raise ValidationError("Payment reference is required")

    response, data = _request_json(
        "GET",
        f"{settings.PAYSTACK_BASE_URL}/transaction/verify/{reference}",
        headers=_build_headers(),
    )

    if response.status_code != 200:
        logger.warning("Paystack verify failed: %s", data)
        raise ValidationError("Payment verification failed. Try again.")
    
    if not data.get("status"):
        raise ValidationError(data.get("message", "Verification failed."))
    
    try:
        payment_data = data["data"]
    except (TypeError, KeyError):
        raise ValidationError("Payment verification failed. Try again.")
    
    logger.info(
        f"Payment verification successful for reference {reference} "
        f"— status: {payment_data.get('status')} "
        f"amount: {payment_data.get('amount')} "   
    )

    return payment_data