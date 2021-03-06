from django_filters import filters
from django_filters.rest_framework import filterset, DjangoFilterBackend

from rest_framework import mixins
from rest_framework import status
from rest_framework.response import Response

from galaxy_ng.app import models
from galaxy_ng.app.api import permissions
from galaxy_ng.app.api import base as api_base
from galaxy_ng.app.api.ui import serializers
from galaxy_ng.app.auth import auth
from galaxy_ng.app.models import auth as auth_models


RH_PE_ACCOUNT_SCOPE = 'system:partner-engineers'


class NamespaceFilter(filterset.FilterSet):
    keywords = filters.CharFilter(method='keywords_filter')

    sort = filters.OrderingFilter(
        fields=(
            ('name', 'name'),
            ('company', 'company'),
            ('id', 'id'),
        ),
    )

    class Meta:
        model = models.Namespace
        fields = ('name', 'company',)

    def keywords_filter(self, queryset, name, value):

        keywords = self.request.query_params.getlist('keywords')

        for keyword in keywords:
            queryset = queryset.filter(name=keyword)

        return queryset


class NamespaceViewSet(
    mixins.ListModelMixin,
    mixins.RetrieveModelMixin,
    mixins.UpdateModelMixin,
    api_base.GenericViewSet,
):
    lookup_field = "name"
    permission_classes = api_base.GALAXY_PERMISSION_CLASSES + [
        permissions.IsNamespaceOwnerOrPartnerEngineer,
    ]

    filter_backends = (DjangoFilterBackend,)
    filterset_class = NamespaceFilter

    def create(self, request, *args, **kwargs):
        groups = []
        for account in request.data['groups']:
            if account == RH_PE_ACCOUNT_SCOPE:
                groups.append(account)
            else:
                if not account.isdigit():
                    return Response(status=status.HTTP_400_BAD_REQUEST)
                group, _ = auth_models.Group.objects.get_or_create_identity(
                    auth.RH_ACCOUNT_SCOPE, account)
                groups.append(group.name)
        request.data['groups'] = groups

        serializer = serializers.NamespaceSerializer(data=request.data)
        if serializer.is_valid():
            serializer.save()
            return Response(serializer.data, status=status.HTTP_201_CREATED)
        return Response(serializer.errors, status=status.HTTP_400_BAD_REQUEST)

    def update(self, request, *args, **kwargs):
        for account in request.data['groups']:
            if account == RH_PE_ACCOUNT_SCOPE:
                continue
            scope, account_id = account.split(':')
            if account_id.isdigit():
                group, _ = auth_models.Group.objects.get_or_create_identity(
                    scope, account_id)
            else:
                return Response(status=status.HTTP_400_BAD_REQUEST)
        if RH_PE_ACCOUNT_SCOPE not in request.data['groups']:
            request.data['groups'].append(RH_PE_ACCOUNT_SCOPE)
        instance = self.get_object()
        serializer = serializers.NamespaceUpdateSerializer(instance, data=request.data)
        serializer.is_valid(raise_exception=True)
        self.perform_update(serializer)

        return Response(serializer.data)

    def perform_update(self, serializer):
        serializer.save()

    def get_serializer_class(self):
        if self.action == 'list':
            return serializers.NamespaceSummarySerializer
        elif self.action == 'update':
            return serializers.NamespaceUpdateSerializer
        else:
            return serializers.NamespaceSerializer

    def get_queryset(self):
        return models.Namespace.objects.all()


class MyNamespaceViewSet(NamespaceViewSet):
    def get_queryset(self):
        # All namespaces for users in the partner-engineers groups

        if permissions.IsPartnerEngineer().has_permission(self.request, self):
            queryset = models.Namespace.objects.all()
            return queryset

        # Just the namespaces with groups the user is in
        queryset = models.Namespace.objects.filter(
            groups__in=self.request.user.groups.all())
        return queryset
