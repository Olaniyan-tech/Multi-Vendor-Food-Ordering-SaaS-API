from rest_framework.exceptions import NotFound
from django_ratelimit.decorators import ratelimit
from django.utils.decorators import method_decorator
import hmac
import hashlib
from food.constants import PAYSTACK_SUCCESS_STATUS
from django.conf import settings
from django.views.decorators.csrf import csrf_exempt
from rest_framework.response import Response
from rest_framework.views import APIView
from rest_framework import generics
from rest_framework import status
from food.models import Category, Food, Order, Vendor, Plan, Subscription
from food.tasks import send_vendor_subscription_payment_email
from .serializers import (
    CategorySerializer,
    FoodSerializer,
    FoodWriteSerializer, 
    OrderSerializer, 
    AddToCartSerializer, 
    OrderDeliveryDetailSerializer, 
    ReviewSerializer,
    VendorRegistrationSerializer,
    VendorProfileSerializer,
    VendorProfileUpdateSerializer,
    VendorDashboardSerializer,
    AdminVendorListSerializer,
    PlanSerializer,
    SubscriptionSerializer,
    SubscriptionHistorySerializer
)
from food.services.cart_service import add_item_to_cart, remove_item_from_cart
from food.services.order_service import ( 
    ADMIN_TRANSITION_MAP,
    VENDOR_TRANSITION_MAP,
    cancel_order, 
    finalize_order,
    update_payment_status
)    
from food.services.payment_service import (
    initialize_order_payment, 
    verify_order_payment,
    initialize_vendor_subscription_payment,
    verify_vendor_subscription_payment
)
from food.services.review_service import (
    create_review, 
    _food_reviews_stats_cache_key,
    _vendor_reviews_stats_cache_key
)
from food.services.vendor_services import (
    register_vendor,
    update_vendor_profile,
    approve_vendor,
    reject_vendor,
    activate_vendor,
    deactivate_vendor,
    create_vendor_food,
    update_vendor_food,
    delete_vendor_food,
    toggle_vendor_food_availability,
)
from food.services.subscription_service import (
    subscribe_vendor,
    cancel_subscription,
    check_subscription_status,
)
from food.selectors import (
    get_all_categories,
    get_category_by_id,
    get_available_foods,
    get_available_food_by_id,
    get_category_by_slug,
    get_user_orders,
    get_pending_order,
    get_order_by_id,
    get_user_order_by_id,
    get_order_by_reference,
    get_order_review, 
    get_food_reviews,
    get_food_reviews_stats,
    get_all_vendors,
    get_vendor_by_slug,
    get_vendor_by_id,
    get_pending_vendors,
    get_vendor_foods,
    get_vendor_orders,
    get_vendor_order_by_id,
    get_vendor_reviews,
    get_vendor_reviews_stats,
    get_vendor_dashboard_stats,
    get_food_by_id,
    get_all_plans,
    get_plan_by_id,
    get_subscription_by_reference,
    get_vendor_subscription_history,
    get_vendor_analytics,
)
from food.filters import FoodFilter, OrderFilter
from rest_framework.pagination import PageNumberPagination
from food.permissions import (
    IsOrderOwner, 
    IsStaff, 
    IsApprovedVendor,
    IsVendorOwner,
    IsAnalyticsAllowed,
)
from rest_framework.permissions import IsAuthenticated, AllowAny
from django.core.exceptions import ValidationError
from django.core.cache import cache
from drf_spectacular.utils import extend_schema
from django.db import transaction
import logging

logger = logging.getLogger(__name__)


class AllFoodView(generics.ListAPIView):
    serializer_class = FoodSerializer
    permission_classes = [AllowAny]
    filterset_class = FoodFilter
    search_fields = ["name", "category__name", "vendor__business_name"]
    ordering_fields = ["price", "name"]
    ordering = ["-created_at"]

    def get_queryset(self):
        vendor_slug = self.request.query_params.get("vendor")
        if vendor_slug:
            try:
                vendor = get_vendor_by_slug(vendor_slug)
            except Vendor.DoesNotExist:
                raise NotFound("Vendor not found")
            return get_available_foods(vendor=vendor)
        return get_available_foods()
    
    def get_serializer_context(self):
        return {"request": self.request}

    @method_decorator(ratelimit(key="ip", rate="60/m", method="GET", block=True))
    def get(self, request, *args, **kwargs):
        return super().get(request, *args, **kwargs)

class FoodDetailView(generics.RetrieveAPIView):
    serializer_class = FoodSerializer
    permission_classes = [AllowAny]

    def get_serializer_context(self):
        return {"request": self.request}

    def get_object(self):
        food_id = self.kwargs["food_id"]
        try:
            return get_available_food_by_id(food_id)
        except Food.DoesNotExist:
            raise NotFound("Food not found")

    @method_decorator(ratelimit(key="ip", rate="30/m", method="GET", block=True))
    def get(self, request, *args, **kwargs):
        return super().get(request, *args, **kwargs)
        

class AllOrdersView(generics.ListAPIView):
    serializer_class = OrderSerializer
    filterset_class = OrderFilter
    ordering_fields = ["created_at", "total"]
    ordering = ["-created_at"]
    
    def get_queryset(self):
        if getattr(self, "swagger_fake_view", False):
            return Order.objects.none()
        return get_user_orders(self.request.user)    

    @method_decorator(ratelimit(key="ip", rate="60/m", method="GET", block=True)) 
    def get(self, request, *args, **kwargs):
        return super().get(request, *args, **kwargs)


class AddToCartView(APIView):

    @extend_schema(request=AddToCartSerializer, responses={201: OrderSerializer})
    @method_decorator(ratelimit(key="user", rate="20/m", method="POST", block=True))
    def post(self, request):
        serializer = AddToCartSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        
        food_id = serializer.validated_data["food"]
        quantity = serializer.validated_data["quantity"]

        try:
            food = get_available_food_by_id(food_id)
        except Food.DoesNotExist:
            return Response({"error": "Food not found"}, status=status.HTTP_404_NOT_FOUND)       

        try:
            order = add_item_to_cart(request.user, food, quantity)
        except ValidationError as e:
            return Response({"error": e.messages[0] if e.messages else str(e)},
                status=status.HTTP_400_BAD_REQUEST
            )
    
        order = get_order_by_id(order.id)
    
        return Response(OrderSerializer(order).data, status=status.HTTP_201_CREATED)


class RemoveFromCartView(APIView):

    @extend_schema(responses={200: OrderSerializer})
    @method_decorator(ratelimit(key="user", rate="10/m", method="POST", block=True))
    def post(self, request):
        user = request.user
        item_id = request.data.get("item_id")
        action = request.data.get("action")  # 'decrease' or 'delete'

        if not item_id:
            return Response({"error": "item_id is required"},
                 status=status.HTTP_400_BAD_REQUEST
            )

        if not action:
            return Response({"error": "Action is required"},
                 status=status.HTTP_400_BAD_REQUEST
            )

        try:
            order = remove_item_from_cart(user=user, item_id=item_id, action=action)
        except ValidationError as e:
            return Response({"error": e.messages[0] if e.messages else str(e)},
                status=status.HTTP_400_BAD_REQUEST
            )       

        if order is None:
            return Response({"message": "Cart is now empty"}, status=status.HTTP_200_OK)
        
        order = get_order_by_id(order.id)
        serializer = OrderSerializer(order)
        return Response(serializer.data, status=status.HTTP_200_OK)
    
   
class CancelOrderView(APIView):

    @extend_schema(responses={200: None})
    @method_decorator(ratelimit(key="user", rate="5/m", method="POST", block=True))
    def post(self, request):
        order = get_pending_order(request.user)
        if not order:
            return Response({"error": "No Order to cancel"}, status=status.HTTP_404_NOT_FOUND)

        try:
            cancel_order(order, user=request.user)
        except ValidationError as e:
            return Response({"error": e.messages[0] if e.messages else str(e)}, 
                status=status.HTTP_400_BAD_REQUEST
            )

        return Response({"message" : "Order cancelled successfully"}, status=status.HTTP_200_OK)

   
class UpdateOrderDetailView(APIView):

    @extend_schema(request=OrderDeliveryDetailSerializer, responses={200: None})
    @method_decorator(ratelimit(key="user", rate="10/m", method="PATCH", block=True))
    def patch(self, request):

        with transaction.atomic():
            order = get_pending_order(request.user)
            if not order:
                return Response({"error" : "No pending order"}, status=status.HTTP_404_NOT_FOUND)

        serializer = OrderDeliveryDetailSerializer(order, data=request.data, partial=True)
        
        serializer.is_valid(raise_exception=True)
        serializer.save()

        return Response({
            "message" : "Order details updated",
            "data": serializer.data}, status=status.HTTP_200_OK)


class CheckOutView(APIView):

    @extend_schema(request=OrderDeliveryDetailSerializer, responses={200: OrderSerializer})
    @method_decorator(ratelimit(key="user", rate="5/m", method="POST", block=True))
    def post(self, request):
        user = request.user

        order = get_pending_order(user=user)
        if not order:
            return Response({"error": "No pending order to checkout"}, 
                status=status.HTTP_404_NOT_FOUND
            )
        
        serializer = OrderDeliveryDetailSerializer(
            instance=order,
            data=request.data,
            context={"request": request},
            partial=True
        )

        if not serializer.is_valid():
            logger.warning(f"Checkout validation failed for user {user.username}: {serializer.errors}")
            return Response(serializer.errors, status=status.HTTP_400_BAD_REQUEST)
        
        serializer.save()

        try:
            order = finalize_order(order, user=user)
        except ValidationError as e:
            return Response({"error": e.messages[0] if e.messages else str(e)}, status=status.HTTP_400_BAD_REQUEST)

        logger.info(f"User {user.username} checkedout for order {order.id} successfully.")
        
        return Response(
            {"message": "Order checked out successfully",
            "user" : user.username,
            "address" : order.address,
            "phone" : order.phone,
            "status" : order.status,
            "total" : order.total}, 
            status=status.HTTP_200_OK
        )


class OrderDetailView(generics.RetrieveAPIView):
    serializer_class = OrderSerializer
    permission_classes = [IsOrderOwner]

    @method_decorator(ratelimit(key="user", rate="10/m", method="GET", block=True))
    def get(self, request, *args, **kwargs):
            return super().get(request, *args, **kwargs)
        
    def get_object(self):
        try:
            order = get_user_order_by_id(self.kwargs["order_id"], self.request.user)
        except Order.DoesNotExist:
            raise NotFound("Order not found")
        self.check_object_permissions(self.request, order)       
        return order


class OrderStatusUpdateView(APIView):
    permission_classes = [IsStaff | IsApprovedVendor]

    @extend_schema(responses={200: OrderSerializer})
    @method_decorator(ratelimit(key="user", rate="30/m", method="PATCH", block=True))
    def patch(self, request, order_id):  
        try:
            order = get_order_by_id(order_id)
        except Order.DoesNotExist:
            return Response({"error": "Order not found!"}, status=status.HTTP_404_NOT_FOUND)
        
        if not request.user.is_staff:
            if order.vendor != request.user.vendor:
                return Response(
                    {"error": "You can only update your own orders."},
                    status=status.HTTP_403_FORBIDDEN
                )
        
        requested_status = request.data.get("status", "").strip().upper()
        if not requested_status:
            return Response({"error": "'status' field is required."}, 
                status=status.HTTP_400_BAD_REQUEST
            )
        
        transition_map = ADMIN_TRANSITION_MAP if request.user.is_staff else VENDOR_TRANSITION_MAP
        handler = transition_map.get(requested_status)
        if not handler:
            return Response({"error": f"'{requested_status}' is not a valid transition.",
                "valid_status": list(transition_map.keys())},
                status=status.HTTP_400_BAD_REQUEST
            )

        try:
            updated_order = handler(order, user=request.user)
        except ValidationError as e:
            return Response({"error": e.messages[0] if e.messages else str(e)},
                status=status.HTTP_409_CONFLICT
            )
        
        return Response({"id": str(updated_order.id), 
            "status": updated_order.status}, 
            status=status.HTTP_200_OK 
        )
    

class InitializeOrderPaymentView(APIView):

    @extend_schema(responses={200: None})
    @method_decorator(ratelimit(key="user", rate="5/m", method="POST", block=True))
    def post(self, request, order_id):
        try:
            order = get_user_order_by_id(order_id, request.user)
        except Order.DoesNotExist:
            return Response({"error": "Order not found!"}, status=status.HTTP_404_NOT_FOUND)
        
        try:
            authorization_url, reference = initialize_order_payment(order)
        except ValidationError as e:
            return Response({"error": e.messages[0] if e.messages else str(e)}, 
                status=status.HTTP_409_CONFLICT
            )
        
        return Response({
            "authorization_url": authorization_url,
            "reference": reference
        }, status=status.HTTP_200_OK)
                

class VerifyOrderPaymentView(APIView):
    
    @extend_schema(responses={200: None})
    @method_decorator(ratelimit(key="user", rate="10/m", method="GET", block=True))
    def get(self, request, reference):
        try:
            order = get_order_by_reference(reference, request.user)
        except Order.DoesNotExist:
            return Response({"error": "Order not found!"}, status=status.HTTP_404_NOT_FOUND)
        
        try:
            payment_data = verify_order_payment(reference)
        except ValidationError as e:
            return Response({"error": e.messages[0] if e.messages else str(e)}, 
                status=status.HTTP_400_BAD_REQUEST
            )
                
        if payment_data["status"] == PAYSTACK_SUCCESS_STATUS:
            update_payment_status(order, "PAID")
            logger.info(f"Payment verified for order {order.id} by {request.user.username}")

            return Response({
                "message": "Payment successful",
                "order_id": order.id,
                "payment_status": order.payment_status,
                "amount_paid": payment_data["amount"] / 100}, 
                status=status.HTTP_200_OK
            )    

        update_payment_status(order, 'FAILED')
        logger.info(f"Payment failed for order {order.id} by {request.user.username}")

        return Response({
            "error": "Payment failed!",
            "payment_status": order.payment_status
        }, status=status.HTTP_402_PAYMENT_REQUIRED)
        

@method_decorator(csrf_exempt, name="dispatch")
class PayStackWebhookView(APIView):
    permission_classes = []

    @extend_schema(exclude=True)
    @method_decorator(ratelimit(key="ip", rate="5/m", method="POST", block=True))
    def post(self, request):
        paystack_signature = request.headers.get("x-paystack-signature")

        if not paystack_signature:
            return Response({"error": "Invalid signature"}, status=status.HTTP_400_BAD_REQUEST)

        computed = hmac.new(
            settings.PAYSTACK_SECRET_KEY.encode("utf-8"),
            request.body,
            hashlib.sha512).hexdigest()
        
        if not hmac.compare_digest(computed, paystack_signature):
            return Response({"error": "Invalid signature"}, status=status.HTTP_400_BAD_REQUEST)

        try: 
            payload = request.data
            event = payload.get("event")
            reference = payload.get("data", {}).get("reference")
        except Exception:
            return Response({"error": "Invalid payload!"}, status=status.HTTP_400_BAD_REQUEST)

        if not reference:
            logger.warning("Webhook received with no reference")
            return Response({"message": "ok"}, status=status.HTTP_200_OK)
        
        try:
            order = Order.objects.get(payment_reference=reference)
        except Order.DoesNotExist:
            logger.warning(f"No order found with reference {reference}")
            return Response({"message": "ok"}, status=status.HTTP_200_OK)

        if event == "charge.success":
            update_payment_status(order, "PAID")
            logger.info(f"Webhook: Payment confirmed for order {order.id}")
        else:
            update_payment_status(order, "FAILED")
            logger.info(f"Webhook: Payment failed for order {order.id} (event: {event})")          
        
        return Response({"message": "ok"}, status=status.HTTP_200_OK)

        
class CreateReviewView(APIView):

    @extend_schema(request=ReviewSerializer, responses={201: ReviewSerializer})
    @method_decorator(ratelimit(key="user", rate="10/m", method="POST", block=True))
    def post(self, request, order_id):
        try:
            order = get_user_order_by_id(order_id, request.user)
        except Order.DoesNotExist:
            return Response({"error": "Order not found!"}, status=status.HTTP_404_NOT_FOUND)
    
        serializer = ReviewSerializer(data=request.data)

        if not serializer.is_valid():
            return Response(serializer.errors, 
                status=status.HTTP_400_BAD_REQUEST
            )
        
        try:
            review = create_review(
                order=order, 
                user=request.user, 
                validated_data=serializer.validated_data
            )
        except ValidationError as e:
            return Response({"error": e.messages[0] if e.messages else str(e)}, 
                status=status.HTTP_400_BAD_REQUEST
            )
        
        logger.info(f"User {request.user.username} reviewed Order {order_id}")

        return Response({
            "message": "Review submitted successfully.",
            "data": ReviewSerializer(review).data}, 
            status=status.HTTP_201_CREATED
        )


class UpdateReviewView(APIView):

    @extend_schema(request=ReviewSerializer, responses={200: ReviewSerializer})
    @method_decorator(ratelimit(key="user", rate="10/m", method="PATCH", block=True))
    def patch(self, request, order_id):
        try:
            order = get_user_order_by_id(order_id, request.user)
        except Order.DoesNotExist:
            return Response({"error": "Order not found!"}, status=status.HTTP_404_NOT_FOUND)

        review = get_order_review(order)
        if not review:
            return Response({"error": "No review found!"}, status=status.HTTP_404_NOT_FOUND)

        serializer = ReviewSerializer(review, data=request.data, partial=True)
        if not serializer.is_valid():
            return Response(serializer.errors, status=status.HTTP_400_BAD_REQUEST)

        serializer.save()
        food_ids = order.items.values_list("food__id", flat=True)
        for food_id in food_ids:
            cache.delete(_food_reviews_stats_cache_key(food_id))
        if order.vendor_id:
            cache.delete(_vendor_reviews_stats_cache_key(order.vendor_id))
        logger.info(f"User {request.user.username} updated review for Order {order_id}")

        return Response({
            "message": "Review updated successfully.",
            "data": serializer.data}, 
            status=status.HTTP_200_OK
        )


class OrderReviewDetailView(APIView):
    
    @extend_schema(responses={200: ReviewSerializer})
    @method_decorator(ratelimit(key="user", rate="5/m", method="GET", block=True))
    def get(self, request, order_id):
        try:
            order = get_user_order_by_id(order_id, request.user)
        except Order.DoesNotExist:
            return Response({"error": "Order not found!"}, status=status.HTTP_404_NOT_FOUND)
        
        review = get_order_review(order)
        if not review:
            return Response({"error": "No review found!"}, status=status.HTTP_404_NOT_FOUND)
        
        return Response(ReviewSerializer(review).data, status=status.HTTP_200_OK)

        
class FoodReviewsView(APIView):
    permission_classes = [AllowAny]

    @extend_schema(responses={200: ReviewSerializer(many=True)})
    @method_decorator(ratelimit(key="ip", rate="5/m", method="GET", block=True))
    def get(self, request, food_id):
        try:
            food = get_available_food_by_id(food_id)
        except Food.DoesNotExist:
            return Response({"error": "Food not found"}, status=status.HTTP_404_NOT_FOUND)        
        
        reviews = get_food_reviews(food_id)

        rating = request.query_params.get("rating")
        min_rating = request.query_params.get("min_rating")
        max_rating = request.query_params.get("max_rating")

        try:
            if rating:
                reviews = reviews.filter(rating=int(rating))        
            if min_rating:
                reviews = reviews.filter(rating__gte=int(min_rating))        
            if max_rating:
                reviews = reviews.filter(rating__lte=int(max_rating))
        
        except ValueError:
            return Response({"error": "Invalid rating value!"}, status=status.HTTP_400_BAD_REQUEST)
        
        paginator = PageNumberPagination()
        paginator.page_size = 10
        paginated_reviews = paginator.paginate_queryset(reviews, request)

        stats = get_food_reviews_stats(food_id)

        paginated_data = paginator.get_paginated_response(
            ReviewSerializer(paginated_reviews, many=True).data
        ).data

        return Response({
            "id": food.id,
            "average_rating": stats["average_rating"],
            "total_reviews": stats["total_reviews"],
            "reviews": paginated_data
        }, status=status.HTTP_200_OK)
                

class VendorListView(generics.ListAPIView):
    serializer_class = VendorProfileSerializer
    permission_classes = [AllowAny]

    def get_queryset(self):
        return get_all_vendors()
    
    def get_serializer_context(self):
        return {"request": self.request}
    
    @method_decorator(ratelimit(key="ip", rate="60/m", method="GET", block=True))
    def get(self, request, *args, **kwargs):
        return super().get(request, *args, **kwargs)

class VendorDetailView(APIView):
    permission_classes = [AllowAny]

    @extend_schema(responses={200: VendorProfileSerializer})
    @method_decorator(ratelimit(key="ip", rate="60/m", method="GET", block=True))
    def get(self, request, slug):
        try:
            vendor = get_vendor_by_slug(slug)
        except Vendor.DoesNotExist:
            return Response({"error": "Vendor not found!"}, status=status.HTTP_404_NOT_FOUND)
        
        stats = get_vendor_reviews_stats(vendor.id)

        serializer = VendorProfileSerializer(
            vendor,
            context={"request": request}
        )

        return Response({
            **serializer.data,
            "average_rating": stats["average_rating"],
            "total_reviews": stats["total_reviews"]}, 
            status=status.HTTP_200_OK
        )


class VendorFoodListView(generics.ListAPIView):
    serializer_class = FoodSerializer
    permission_classes = [AllowAny]
    filterset_class = FoodFilter
    search_fields = ["name", "category__name"]
    ordering_fields = ["price", "name"]
    ordering = ["-created_at"]

    def get_queryset(self):
        slug = self.kwargs["slug"]
        try:
            vendor = get_vendor_by_slug(slug)
        except Vendor.DoesNotExist:
            raise NotFound("Vendor not found!")
        return get_available_foods(vendor=vendor)

    def get_serializer_context(self):
        return {"request": self.request}
    
    @method_decorator(ratelimit(key="ip", rate="60/m", method="GET", block=True))
    def get(self, request, *args, **kwargs):
        return super().get(request, *args, **kwargs)
    

class VendorReviewsView(APIView):
    permission_classes = [AllowAny]

    @extend_schema(responses={200: ReviewSerializer(many=True)})
    @method_decorator(ratelimit(key="ip", rate="30/m", method="GET", block=True))
    def get(self, request, slug):
        try:
            vendor = get_vendor_by_slug(slug)
        except Vendor.DoesNotExist:
            return Response(
                {"error": "Vendor not found!"},
                status=status.HTTP_404_NOT_FOUND
            )
        
        reviews = get_vendor_reviews(vendor)

        rating = request.query_params.get("rating")
        min_rating = request.query_params.get("min_rating")
        max_rating = request.query_params.get("max_rating")

        try:
            if rating:
                reviews = reviews.filter(rating=int(rating))        
            if min_rating:
                reviews = reviews.filter(rating__gte=int(min_rating))        
            if max_rating:
                reviews = reviews.filter(rating__lte=int(max_rating))

        except ValueError:
            return Response({"error": "Invalid rating value!"}, status=status.HTTP_400_BAD_REQUEST)
    
        paginator = PageNumberPagination()
        paginator.page_size = 10
        paginated_reviews = paginator.paginate_queryset(reviews, request)

        stats = get_vendor_reviews_stats(vendor.id)

        paginated_data = paginator.get_paginated_response(
            ReviewSerializer(paginated_reviews, many=True).data
        ).data

        return Response({
            "average_rating": stats["average_rating"],
            "total_reviews": stats["total_reviews"],
            "reviews": paginated_data}, 
            status=status.HTTP_200_OK
        )


class VendorRegistrationView(APIView):
    permission_classes = [IsAuthenticated]

    @extend_schema(request=VendorRegistrationSerializer, responses={201: VendorProfileSerializer})
    @method_decorator(ratelimit(key="user", rate="10/m", method="POST", block=True))
    def post(self, request):
        serializer = VendorRegistrationSerializer(
            data=request.data, 
            context={"request": request}
        )

        if not serializer.is_valid():
            return Response(serializer.errors, status=status.HTTP_400_BAD_REQUEST)
        
        try:
            vendor = register_vendor(
                user=request.user,
                validated_data=serializer.validated_data
            )
        except ValidationError as e:
            return Response({
                "error": e.messages[0] if e.messages else str(e)}, 
                status=status.HTTP_400_BAD_REQUEST
            )

        logger.info(f"New vendor application: {vendor.business_name} by {request.user.username}")

        return Response({
            "message": "Vendor application submitted successfully. Awaiting admin approval.",
            "data": VendorProfileSerializer(vendor, context={"request": request}).data}, 
            status=status.HTTP_201_CREATED
        )


class VendorDashboardView(APIView):
    permission_classes = [IsApprovedVendor]

    @extend_schema(responses={200: VendorDashboardSerializer})
    @method_decorator(ratelimit(key="user", rate="30/m", method="GET", block=True))
    def get(self, request):
        vendor = request.user.vendor

        serializer = VendorDashboardSerializer(
            vendor,
            context={"request": request}
        )

        return Response(serializer.data, status=status.HTTP_200_OK)


class VendorDashboardStatsView(APIView):
    permission_classes = [IsApprovedVendor]

    @method_decorator(ratelimit(key="user", rate="30/m", method="GET", block=True))
    def get(self, request):
        vendor = request.user.vendor
        stats = get_vendor_dashboard_stats(vendor)
        return Response(stats, status=status.HTTP_200_OK)


class VendorProfileUpdateView(APIView):
    permission_classes = [IsApprovedVendor]

    @extend_schema(request=VendorProfileUpdateSerializer, responses={200: VendorDashboardSerializer})
    @method_decorator(ratelimit(key="user", rate="10/m", method="PATCH", block=True))
    def patch(self, request):
        vendor = request.user.vendor

        serializer = VendorProfileUpdateSerializer(
            vendor,
            data=request.data,
            partial=True,
            context={"request": request}
        )

        if not serializer.is_valid():
            return Response(serializer.errors, status=status.HTTP_400_BAD_REQUEST)
        
        try:
            vendor = update_vendor_profile(
                vendor=vendor,
                validated_data=serializer.validated_data
            )
        except ValidationError as e:
            return Response({"error": e.messages[0] if e.messages else str(e)}, 
            status=status.HTTP_400_BAD_REQUEST
        )

        logger.info(f"Vendor {vendor.business_name} updated their profile")

        return Response({
            "message": "Profile updated successfully",
            "data": VendorDashboardSerializer(vendor, context={"request":request}).data}, 
            status=status.HTTP_200_OK
        )


class VendorFoodCreateView(APIView):
    permission_classes = [IsApprovedVendor]

    @extend_schema(request=FoodWriteSerializer, responses={201: FoodSerializer})
    @method_decorator(ratelimit(key="user", rate="20/m", method="POST", block=True))
    def post(self, request):
        vendor = request.user.vendor
        serializer = FoodWriteSerializer(
            data=request.data,
            context={"request": request}
        )

        if not serializer.is_valid():
            return Response(serializer.errors, status=status.HTTP_400_BAD_REQUEST)
        
        try:
            food = create_vendor_food(
                vendor=vendor,
                validated_data=serializer.validated_data
            )
        except ValidationError as e:
            return Response({"error": e.messages[0] if e.messages else str(e)}, 
            status=status.HTTP_400_BAD_REQUEST
        )

        logger.info(f"Vendor {vendor.business_name} created food: {food.name}")

        return Response({
            "message": f"{food.name} created successfully.",
            "data": FoodSerializer(food, context={"request": request}).data},
            status=status.HTTP_201_CREATED
        )


class VendorFoodsView(generics.ListAPIView):
    serializer_class = FoodSerializer
    permission_classes = [IsApprovedVendor]
    filterset_class = FoodFilter
    ordering_fields = ["price", "name"]
    ordering = ["-created_at"]

    def get_queryset(self):
        return get_vendor_foods(self.request.user.vendor)

    def get_serializer_context(self):
        return {"request": self.request}
    
    @method_decorator(ratelimit(key="ip", rate="60/m", method="GET", block=True))
    def get(self, request, *args, **kwargs):
        return super().get(request, *args, **kwargs)


class VendorFoodDetailView(APIView):
    permission_classes = [IsApprovedVendor, IsVendorOwner]

    # helper method — reused by patch() and delete()
    # avoids repeating the same try/except in both methods
    def get_food(self, request, food_id):
        try:
            food = get_food_by_id(food_id)
        except Food.DoesNotExist:
            raise NotFound("Food not found!")

        self.check_object_permissions(request, food)
        return food
    
    @extend_schema(responses={200: FoodSerializer})
    @method_decorator(ratelimit(key ="user", rate="30/m", method="GET", block=True))
    def get(self, request, food_id):
        food = self.get_food(request, food_id)
        return Response(
            FoodSerializer(food, context={"request": request}).data, 
            status=status.HTTP_200_OK
        )
    

    @extend_schema(request=FoodWriteSerializer, responses={200: FoodSerializer})
    @method_decorator(ratelimit(key="user", rate="20/m", method="PATCH", block=True))
    def patch(self, request, food_id):
        food = self.get_food(request, food_id)
        serializer = FoodWriteSerializer(
            food,
            data=request.data,
            partial=True,
            context={"request": request}
        )
        
        if not serializer.is_valid():
            return Response(serializer.errors, status=status.HTTP_400_BAD_REQUEST)
        
        try:
            food = update_vendor_food(
                food,
                user=request.user,
                validated_data=serializer.validated_data
            )
        except ValidationError as e:
            return Response({"error": e.messages[0] if e.messages else str(e)},
                status=status.HTTP_400_BAD_REQUEST
            )

        logger.info(f"Vendor {request.user.vendor.business_name} updated food: {food.name}")

        return Response({
            "message": "Food updated successfully.",
            "data": FoodSerializer(food, context={"request": request}).data}, 
            status=status.HTTP_200_OK
        )

    @extend_schema(responses={204: None})
    @method_decorator(ratelimit(key="user", rate="10/m", method="DELETE", block=True))
    def delete(self, request, food_id):
        food = self.get_food(request, food_id)
        try:
            result = delete_vendor_food(food)
        except ValidationError as e:
            return Response(
                {"error": e.messages[0] if e.messages else str(e)},
                status=status.HTTP_400_BAD_REQUEST
            )

        logger.info(f"Vendor {request.user.vendor.business_name} deleted food: {food.name}")

        if result == "hidden":
            return Response({
                "message": f"{food.name} cannot be deleted because it has existing orders. It has been marked as unavailable instead."},
                status=status.HTTP_200_OK
            )

        return Response({
            "message": f"{food.name} deleted successfully."},
            status=status.HTTP_204_NO_CONTENT
        )


class VendorFoodToggleAvailabilityView(APIView):
    # Vendor marks food as available/unavailable
    # Separate endpoint — single purpose, clear intent
    # e.g. "Zinger Burger is sold out for today"
    permission_classes = [IsApprovedVendor, IsVendorOwner]

    @method_decorator(ratelimit(key="user", rate="20/m", method="PATCH", block=True))
    @extend_schema(responses={200: None})
    def patch(self, request, food_id):
        try:
            food = get_food_by_id(food_id)
        except Food.DoesNotExist:
            return Response(
                {"error": "Food not found!"},
                status=status.HTTP_404_NOT_FOUND
            )
            
        self.check_object_permissions(request, food)
    
        try:
            food = toggle_vendor_food_availability(food)
        except ValidationError as e:
            return Response(
                {"error": e.messages[0] if e.messages else str(e)},
                status=status.HTTP_400_BAD_REQUEST
            )

        logger.info(
            f"{food.name} is now {'available' if food.available else 'unavailable'}"
            f" - Vendor {request.user.vendor.business_name}"
        )
        
        return Response({
            "message": f"{food.name} is now {'available' if food.available else 'unavailable'}",
            "available": food.available}, 
            status=status.HTTP_200_OK
        )

class VendorOrderListView(generics.ListAPIView):
    serializer_class = OrderSerializer
    permission_classes = [IsApprovedVendor]
    filterset_class = OrderFilter
    ordering_fields = ["created_at", "total"]
    ordering = ["-created_at"]

    def get_queryset(self):
        if getattr(self, "swagger_fake_view", False):
            return Order.objects.none()
        return get_vendor_orders(self.request.user.vendor)
    
    @method_decorator(ratelimit(key="user", rate="60/m", method="GET", block=True))
    def get(self, request, *args, **kwargs):
        return super().get(request, *args, **kwargs)

class VendorOrderDetailView(APIView):
    permission_classes = [IsApprovedVendor]

    @extend_schema(responses={200: OrderSerializer})
    @method_decorator(ratelimit(key="user", rate="30/m", method="GET", block=True))
    def get(self, request, order_id):
        vendor = request.user.vendor
        try:
            order = get_vendor_order_by_id(
                vendor=vendor,
                order_id=order_id,
            )
        except Order.DoesNotExist:
            return Response({
                "error": 'Order not found!'},
                status=status.HTTP_404_NOT_FOUND
            )

        return Response(
            OrderSerializer(order, context={"request": request}).data,
            status=status.HTTP_200_OK
        )

class AdminVendorListView(generics.ListAPIView):
    serializer_class = AdminVendorListSerializer
    permission_classes = [IsStaff]
    ordering_fields = ["created_at", "business_name"]
    ordering = ["-created_at"]

    def get_queryset(self):
        if getattr(self, "swagger_fake_view", False):
            return Vendor.objects.none()
        
        status_filter = self.request.query_params.get("status")

        if status_filter == "pending":
            return get_pending_vendors()
        if status_filter == "active":
            return get_all_vendors()
        
        return Vendor.objects.all()
    
    def get_serializer_context(self):
        return {"request": self.request}
        
    @method_decorator(ratelimit(key="user", rate="30/m", method="GET", block=True))
    def get(self, request, *args, **kwargs):
        return super().get(request, *args, **kwargs)


class AdminVendorDetailView(APIView):
    permission_classes = [IsStaff]

    @extend_schema(responses={200: VendorDashboardSerializer})
    @method_decorator(ratelimit(key="user", rate="30/m", method="GET", block=True))
    def get(self, request, vendor_id):
        try:
            vendor = get_vendor_by_id(vendor_id)
        except Vendor.DoesNotExist:
            return Response(
                {"error": "Vendor not found!"},
                status=status.HTTP_404_NOT_FOUND
            )
        
        stats = get_vendor_reviews_stats(vendor.id)
        dashboard_stats = get_vendor_dashboard_stats(vendor)

        return Response({
            **VendorDashboardSerializer(vendor, context={"request": request}).data,
            "review_stats": stats,
            "dashboard_stats": dashboard_stats},
            status=status.HTTP_200_OK
        )


class AdminApproveVendorView(APIView):
    permission_classes = [IsStaff]

    @extend_schema(responses={200: VendorDashboardSerializer})
    @method_decorator(ratelimit(key="user", rate="20/m", method="PATCH", block=True))

    def patch(self, request, vendor_id):
        try:
            vendor = get_vendor_by_id(vendor_id)
        except Vendor.DoesNotExist:
                return Response({
                    "error": "Vendor not found!"},
                status=status.HTTP_404_NOT_FOUND
            )

        try:
            vendor = approve_vendor(vendor=vendor, approved_by=request.user)
        except ValidationError as e:
            return Response({
                "error": e.messages[0] if e.messages else str(e)},
                status=status.HTTP_400_BAD_REQUEST
            )

        logger.info(
            f"Admin {request.user.username} approved vendor: {vendor.business_name}"
        )

        return Response({
            "message": f"Vendor {vendor.business_name} has been approved successfully.",
            "data": VendorDashboardSerializer(vendor, context={"request": request}).data},
            status=status.HTTP_200_OK
        )


class AdminVendorRejectView(APIView):
    # Admin rejects a vendor application
    # Vendor cannot access dashboard
    permission_classes = [IsStaff]

    @extend_schema(responses={200: None})
    @method_decorator(ratelimit(key="user", rate="20/m", method="PATCH", block=True))
    def patch(self, request, vendor_id):
        try:
            vendor = get_vendor_by_id(vendor_id)
        except Vendor.DoesNotExist:
            return Response(
                {"error": "Vendor not found!"},
                status=status.HTTP_404_NOT_FOUND
            )
        
        try:
            vendor = reject_vendor(vendor=vendor, rejected_by=request.user)
        except ValidationError as e:
            return Response({
                "error": e.messages[0] if e.messages else str(e)},
                status=status.HTTP_400_BAD_REQUEST
            )

        logger.info(f"Admin {request.user.username} rejected vendor {vendor.business_name}")

        return Response({
            "message": f"Vendor {vendor.business_name} application has been rejected."},
            status=status.HTTP_200_OK
        )


class AdminVendorActivateView(APIView):
    permission_classes = [IsStaff]

    @extend_schema(responses={200: None})
    @method_decorator(ratelimit(key="user", rate="20/m", method="PATCH", block=True))
    def patch(self, request, vendor_id):
        try:
            vendor = get_vendor_by_id(vendor_id)
        except Vendor.DoesNotExist:
            return Response({
                "error": "Vendor not found!"},
                status=status.HTTP_404_NOT_FOUND
            )

        try:
            vendor = activate_vendor(vendor=vendor, activated_by=request.user)
        except ValidationError as e:
            return Response({
                "error": e.messages[0] if e.messages else str(e)},
                status=status.HTTP_400_BAD_REQUEST
            )
        
        logger.info(f"Admin {request.user.username} activated vendor: {vendor.business_name}")

        return Response({
            "message": f"Vendor {vendor.business_name} has been activated."},
            status=status.HTTP_200_OK
        )


class AdminVendorDeactivateView(APIView):
    # Admin deactivates an approved vendor
    # Use case: vendor violates terms, poor performance
    # Vendor loses access to dashboard immediately
    permission_classes = [IsStaff]

    @extend_schema(responses={200: None})
    @method_decorator(ratelimit(key="user", rate="20/m", method="PATCH", block=True))
    def patch(self, request, vendor_id):
        try:
            vendor = get_vendor_by_id(vendor_id)
        except Vendor.DoesNotExist:
            return Response(
                {"error": "Vendor not found!"},
                status=status.HTTP_404_NOT_FOUND
            )

        try:
            vendor = deactivate_vendor(vendor=vendor, deactivated_by=request.user)
        except ValidationError as e:
            return Response(
                {"error": e.messages[0] if e.messages else str(e)},
                status=status.HTTP_400_BAD_REQUEST
            )

        logger.info(
            f"Admin {request.user.username} deactivated vendor: {vendor.business_name}"
        )

        return Response({
            "message": f"Vendor {vendor.business_name} has been deactivated."}, 
            status=status.HTTP_200_OK
        )


class CategoryListView(generics.ListAPIView):
    serializer_class = CategorySerializer
    permission_classes = [AllowAny]

    def get_queryset(self):
        return get_all_categories()
    
    @method_decorator(ratelimit(key="ip", rate="60/m", method="GET", block=True))
    def get(self, request, *args, **kwargs):
        return super().get(request, *args, **kwargs)


class CategoryFoodsView(generics.ListAPIView):
    # all available foods under a category
    serializer_class = FoodSerializer
    permission_classes = [AllowAny]
    filterset_class = FoodFilter

    def get_queryset(self):
        slug = self.kwargs["slug"]
        try:
            category = get_category_by_slug(slug)
        except Category.DoesNotExist:
            raise NotFound("Category not found")
        return get_available_foods().filter(category=category)


class AdminCategoryCreateView(APIView):
    permission_classes = [IsStaff]

    @method_decorator(ratelimit(key="user", rate="20/m", method="POST", block=True))
    @extend_schema(request=CategorySerializer, responses={201: CategorySerializer})
    def post(self, request):
        serializer = CategorySerializer(data=request.data)
        if not serializer.is_valid():
            return Response(serializer.errors, status=400)
        category = serializer.save()
        return Response(
            CategorySerializer(category).data,
            status=201
        )



class AdminCategoryDetailView(APIView):
    permission_classes = [IsStaff]

    @method_decorator(ratelimit(key="user", rate="20/m", method="GET", block=True))
    @extend_schema(responses={200: CategorySerializer})
    def get(self, request, category_id):
        try:
            category = get_category_by_id(category_id)
        except Category.DoesNotExist:
            raise NotFound("Category not found")
        return Response(CategorySerializer(category).data)
    
    @method_decorator(ratelimit(key="user", rate="20/m", method="PATCH", block=True))
    @extend_schema(request=CategorySerializer, responses={200: CategorySerializer})
    def patch(self, request, category_id):
        try:
            category = get_category_by_id(category_id)
        except Category.DoesNotExist:
            raise NotFound("Category not found")
        serializer = CategorySerializer(
            category, data=request.data, partial=True
        )
        if not serializer.is_valid():
            return Response(serializer.errors, status=400)
        category = serializer.save()
        return Response(CategorySerializer(category).data)

    @method_decorator(ratelimit(key="user", rate="20/m", method="DELETE", block=True))
    @extend_schema(responses={204: None})
    def delete(self, request, category_id):
        try:
            category = get_category_by_id(category_id)
        except Category.DoesNotExist:
            raise NotFound("Category not found")
        category.delete()
        return Response(
            {"message": "Category deleted successfully"},
            status=204
        )


class PlanListView(generics.ListAPIView):
    serializer_class = PlanSerializer
    permission_classes = [AllowAny]

    @method_decorator(ratelimit(key="ip", rate="60/m", method="GET", block=True))
    @extend_schema(responses={200: PlanSerializer(many=True)})
    def get(self, request, *args, **kwargs):
            return super().get(request, *args, *kwargs)

    def get_queryset(self):
        return get_all_plans()
    

class VendorSubscriptionVIew(APIView):
    permission_classes = [IsApprovedVendor]

    @method_decorator(ratelimit(key="user", rate="30/m", method="GET", block=True))
    @extend_schema(responses={200: SubscriptionSerializer})
    def get(self, request):        
        vendor = request.user.vendor
        subscription = check_subscription_status(vendor)
        
        return Response(
            SubscriptionSerializer(subscription).data,
            status=status.HTTP_200_OK
        )
        

class CancelSubscriptionView(APIView):
    permission_classes = [IsVendorOwner]

    @method_decorator(ratelimit(key="user", rate="5/m", method="DELETE", block=True))
    @extend_schema(responses={200: None})
    def delete(self, request):
        try:
            cancel_subscription(request.user.vendor)
        except ValidationError as e:
            return Response({
                "error": e.messages[0] if e.messages[0] else str(e)},
                status=status.HTTP_400_BAD_REQUEST
            )
        
        logger.info(f"Vendor {request.user.vendor.business_name} cancelled subscription")

        return Response({
            "message": "Subsription cancelled. You are now on the free plan."},
            status=status.HTTP_200_OK
        )


class VendorSubscriptionHistoryView(generics.ListAPIView):
    serializer_class = SubscriptionHistorySerializer
    permission_classes = [IsApprovedVendor]

    @method_decorator(ratelimit(key="user", rate="30/m", method="GET", block=True))
    @extend_schema(responses={200: SubscriptionHistorySerializer(many=True)})
    def get(self, request, *args, **kwargs):
        return super().get(request, *args, **kwargs)
    
    def get_queryset(self):
        return get_vendor_subscription_history(self.request.user.vendor)


class VendorAnalyticsView(APIView):
    permission_classes = [IsApprovedVendor, IsAnalyticsAllowed]

    @method_decorator(ratelimit(key="user", rate="30/m", method="GET", block=True))
    @extend_schema(responses={200: None})
    def get(self, request):
        vendor = request.user.vendor
        analytics = get_vendor_analytics(vendor)
        return Response(analytics, status=status.HTTP_200_OK)


class InitializeVendorSubscriptionPaymentView(APIView):
    permission_classes = [IsApprovedVendor]

    @method_decorator(ratelimit(key="user", rate="5/m", method="POST", block=True))
    @extend_schema(responses={200: None})
    def post(self, request):
        plan_id = request.data.get("plan_id")
        if not plan_id:
            return Response(
                {"error": "plan_id is required"},
                status=status.HTTP_400_BAD_REQUEST
            )

        try:
            plan = get_plan_by_id(plan_id)
        except Plan.DoesNotExist:
            return Response(
                {"error": "Plan not found"},
                status=status.HTTP_404_NOT_FOUND
            )

        try:
            authorization_url, reference = initialize_vendor_subscription_payment(
                vendor=request.user.vendor,
                plan=plan
            )
        except ValidationError as e:
            return Response(
                {"error": e.messages[0] if e.messages else str(e)},
                status=status.HTTP_409_CONFLICT
            )
        
        logger.info(
            f"Vendor {request.user.vendor.business_name} "
            f"initialized payment for {plan.name} plan"
        )

        return Response({
            "authorization_url": authorization_url,
            "reference": reference,
            "plan": plan.name,
            "amount": plan.price,
        }, status=status.HTTP_200_OK)


class VerifyVendorSubscriptionPaymentView(APIView):
    permission_classes = [IsApprovedVendor]

    @method_decorator(ratelimit(key="user", rate="10/m", method="GET", block=True))
    @extend_schema(responses={200: SubscriptionSerializer})
    def get(self, request, reference):
        vendor = request.user.vendor

        try:
            subscription = get_subscription_by_reference(reference, vendor)
            return Response(
                SubscriptionSerializer(subscription).data,
                status=status.HTTP_200_OK
            )
        except Subscription.DoesNotExist:
            pass

        try:
            payment_data = verify_vendor_subscription_payment(reference)
        except ValidationError as e:
            return Response({"error": e.messages[0] if e.messages else str(e)}, 
                status=status.HTTP_400_BAD_REQUEST
            )
        
        if payment_data["status"] != PAYSTACK_SUCCESS_STATUS:
            logger.warning(
                f"Payment failed for vendor {vendor.id} ref={reference}"
            )
            return Response(
                {"error": "Payment was not successful"},
                status=status.HTTP_402_PAYMENT_REQUIRED
            )
        
        metadata = payment_data.get("metadata", {})
        plan_id = metadata.get("plan_id")

        if not plan_id:
            return Response(
                {"error": "Plan ID missing in payment metadata"},
                status=status.HTTP_400_BAD_REQUEST
            )

        try:
            plan = get_plan_by_id(plan_id)
        except Plan.DoesNotExist:
            return Response(
                {"error": "Plan not found"},
                status=status.HTTP_404_NOT_FOUND
            )

         # activate subscription
        try:
            subscription = subscribe_vendor(
                vendor=request.user.vendor,
                plan_id=plan_id,
                payment_reference=reference
            )
        except ValidationError as e:
            return Response(
                {"error": e.messages[0] if e.messages else str(e)},
                status=status.HTTP_400_BAD_REQUEST
            )
        
        send_vendor_subscription_payment_email.delay(vendor.id, plan.name, reference)

        logger.info(
            f"Vendor {request.user.vendor.business_name} "
            f"activated {plan.name} subscription"
        )

        return Response({
            "message": "You subscribed successfully",
            "data": SubscriptionSerializer(subscription).data},
            status=status.HTTP_200_OK
        )