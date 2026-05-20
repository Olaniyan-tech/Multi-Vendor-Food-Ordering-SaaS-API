from rest_framework import serializers
from food.models import(
    Food, 
    Order, 
    OrderItem, 
    Review, 
    Category, 
    Vendor,
    Plan, 
    Subscription,
    SubscriptionHistory,
)
from users.validators import validate_phone_format
from drf_spectacular.utils import extend_schema_field


def validate_vendor_phone(value, instance):
    validate_phone_format(value)

    queryset = Vendor.objects.filter(phone=value)

    if instance:
        queryset = queryset.exclude(id=instance.id)
    
    if queryset.exists():
        raise serializers.ValidationError("This phone number is already used by another vendor")
    
    return value


class CategorySerializer(serializers.ModelSerializer):
    class Meta:
        model = Category
        fields = ("id", "name", "slug")


class VendorRegistrationSerializer(serializers.ModelSerializer):
    class Meta:
        model = Vendor
        fields = (
            "business_name",
            "description",
            "profile_photo",
            "phone",
            "address",
            "city",
            "state",
            "country",
        )
    
    def validate_phone(self, value):
        return validate_vendor_phone(value, self.instance)


class VendorProfileSerializer(serializers.ModelSerializer):
    class Meta:
        model = Vendor
        fields = (
            "id", 
            "business_name",
            "description",
            "profile_photo",
            "slug",
        )


class VendorProfileUpdateSerializer(serializers.ModelSerializer):
    class Meta:
        model = Vendor
        fields = (
            "business_name",
            "description",
            "profile_photo",
            "phone",
            "address",
            "city",
            "state",
            "country",
            "slug",
            "is_active",   
            "is_approved"
        )
        read_only_fields = ("slug", "is_active", "is_approved")


    def validate_phone(self, value):
        return validate_vendor_phone(value, self.instance)
   

class VendorDashboardSerializer(serializers.ModelSerializer):
    is_approved = serializers.BooleanField(read_only=True)

    class Meta:
        model = Vendor
        fields = (
            "id",
            "business_name",
            "slug",
            "description",
            "profile_photo",
            "phone",
            "address",
            "city",
            "state",
            "country",
            "is_active",
            "is_approved",
            "created_at",
            "updated_at",
        )
        read_only_fields = ("slug", "is_active", "created_at", "updated_at")


    def validate_phone(self, value):
        return validate_vendor_phone(value, self.instance)


class AdminVendorListSerializer(serializers.ModelSerializer):
    class Meta:
        model = Vendor
        fields = (
            "id",
            "business_name",
            "slug",
            "profile_photo",
            "city",
            "state",
            "is_active",      
            "is_approved",    
            "created_at",       
        )


class FoodSerializer(serializers.ModelSerializer):
    category = CategorySerializer(read_only=True)
    vendor = VendorProfileSerializer(read_only=True)
    image = serializers.SerializerMethodField()

    class Meta:
        model = Food
        fields = (
            "id", 
            "vendor", 
            "name", 
            "slug",
            "price", 
            "description", 
            "image", 
            "category", 
            "available",
            "stock"
        )
    
    @extend_schema_field(serializers.CharField(allow_null=True))
    def get_image(self, obj):
        request = self.context.get("request")
        if obj.image:
            if request:
                return request.build_absolute_uri(obj.image.url)
            return obj.image.url
        return None


class FoodWriteSerializer(serializers.ModelSerializer):
    class Meta:
        model = Food
        fields = (
            "category", 
            "name",  
            "description", 
            "price",
            "image",
            "available", 
            "stock",
        )
    
    def validate_price(self, value):
        if value <= 0:
            raise serializers.ValidationError("Price must be greater than 0")
        return value
    
    def validate_stock(self, value):
        if value < 0:
            raise serializers.ValidationError("Stock cannot be negative")
        return value


class CartFoodSerializer(serializers.ModelSerializer):
    class Meta:
        model = Food
        fields = (
            "id",
            "name",
            "slug",
            "price",
            "image",
        )

class OrderItemSerializer(serializers.ModelSerializer):
    food = CartFoodSerializer(read_only=True)
    subtotal = serializers.SerializerMethodField()

    class Meta:
        model = OrderItem
        fields = ("id", "food", "quantity", "price_at_purchase", "subtotal")
    
    @extend_schema_field(serializers.DecimalField(max_digits=10, decimal_places=2))
    def get_subtotal(self, obj):
        return obj.subtotal


class OrderSerializer(serializers.ModelSerializer):
    items = OrderItemSerializer(many=True, read_only=True)
    user = serializers.CharField(source='user.username', read_only=True)
    vendor = VendorProfileSerializer(read_only=True)

    class Meta:
        model = Order
        fields = (
            "id", 
            "user", 
            "vendor", 
            "address", 
            "phone", 
            "total", 
            "status", 
            "payment_status", 
            "created_at", 
            "updated_at", 
            "items"
        )


class AddToCartSerializer(serializers.Serializer):
    food = serializers.IntegerField()
    quantity = serializers.IntegerField(min_value=1, default=1)

   
class OrderDeliveryDetailSerializer(serializers.ModelSerializer):

    class Meta:
        model = Order
        fields = ("address", "phone")
    
    def validate_address(self, value):
        if not value:
            raise serializers.ValidationError("Address is required for checkout.")
        return value
    
    def validate_phone(self, value):
        validate_phone_format(value)
        return value

    
    def update(self, instance, validated_data):
        instance.address = validated_data.get("address", instance.address)

        if not instance.address:
            raise serializers.ValidationError("Address is required")

        phone = validated_data.get("phone")
        if not phone:
            if instance.phone:
                phone = instance.phone
            elif hasattr(instance.user, "profile") and instance.user.profile.phone:
                phone = instance.user.profile.phone
            else:
                raise serializers.ValidationError("Phone number is required.")
        
        instance.phone = phone
        instance.save(update_fields=["address", "phone"])
        return instance


class ReviewSerializer(serializers.ModelSerializer):
    user = serializers.CharField(source="user.username", read_only=True)
    vendor = VendorProfileSerializer(read_only=True)

    class Meta:
        model = Review
        fields = (
            "id", 
            "user", 
            "vendor",
            "order", 
            "rating", 
            "comment", 
            "photo", 
            "created_at", 
            "updated_at"
        )
        read_only_fields = ("order",)

    def validate(self, data):
        order = self.instance.order if self.instance else data.get("order")
        if order:
            data["vendor"] = order.vendor
        return data


class PlanSerializer(serializers.ModelSerializer):
    class Meta:
        model = Plan
        fields = (
            "id",
            "name",
            "price",
            "max_food_listings",
            "max_orders_per_month",
            "can_receive_reviews",
            "priority_listing",
            "analytics_access",
            "description"
        )


class SubscriptionSerializer(serializers.ModelSerializer):
    plan = PlanSerializer(read_only=True)
    is_valid = serializers.SerializerMethodField()
    business_name = serializers.CharField(source="vendor.business_name", read_only=True)

    class Meta:
        model = Subscription
        fields = (
            "id",
            "business_name",
            "plan",
            "status",
            "start_date",
            "end_date",
            "is_valid",
            "created_at"
        )

    def get_is_valid(self, obj):
        return obj.is_valid()


class SubscriptionHistorySerializer(serializers.ModelSerializer):
    plan_name = serializers.CharField(source="plan.name", read_only=True)

    class Meta:
        model = SubscriptionHistory
        fields = ["id", "plan_name", "event", "payment_reference", "created_at"]
    
        
